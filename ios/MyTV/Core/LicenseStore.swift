import Combine
import CryptoKit
import Foundation
import Security

public struct LicenseTicket: Codable, Hashable {
    public var v: Int
    public var code: String
    public var plan: String
    public var days: Int
    public var issued: String
    public var nonce: String
    public var sig: String

    public init(v: Int, code: String, plan: String, days: Int, issued: String, nonce: String, sig: String) {
        self.v = v
        self.code = code
        self.plan = plan
        self.days = days
        self.issued = issued
        self.nonce = nonce
        self.sig = sig
    }

    init?(json: [String: Any]) {
        guard let v = (json["v"] as? NSNumber)?.intValue,
              let code = json["code"] as? String,
              let plan = json["plan"] as? String,
              let days = (json["days"] as? NSNumber)?.intValue,
              let issued = json["issued"] as? String,
              let nonce = json["nonce"] as? String,
              let sig = json["sig"] as? String else {
            return nil
        }
        self.init(v: v, code: code, plan: plan, days: days, issued: issued, nonce: nonce, sig: sig)
    }

    var canonicalMessage: Data {
        Data("1|\(code)|\(plan)|\(days)|\(issued)|\(nonce)".utf8)
    }
}

public enum LicenseTicketVerifier {
    private static let productionPublicKeyBase64 =
        "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEwJd4v53jwXWpinnG1qgVU3D9gM0VvvEFB83Bm/sAEwTcFnISEOJu8DyvyiEW7q9fR9Si7M0afjbMt6k3IOw1hw=="
    private static let planDays: [String: Int] = [
        "weekly": 7,
        "monthly": 30,
        "quarterly": 90,
        "yearly": 365,
        "lifetime": 0
    ]

    public static func verify(
        _ ticket: LicenseTicket,
        expectedCode: String? = nil,
        publicKeyDER: Data? = nil
    ) -> Bool {
        let code = ticket.code.uppercased()
        guard let signatureData = Data(base64Encoded: ticket.sig) else { return false }
        let keyData = publicKeyDER ?? (Data(base64Encoded: productionPublicKeyBase64) ?? Data())
        guard ticket.v == 1,
              expectedCode.map({ $0.uppercased() == code }) ?? true,
              planDays[ticket.plan] == ticket.days,
              ticket.issued.count == 10,
              ticket.issued.allSatisfy({ $0.isNumber }),
              ticket.nonce.count >= 8,
              !keyData.isEmpty,
              let publicKey = try? P256.Signing.PublicKey(derRepresentation: keyData),
              let signature = decodeSignature(signatureData) else {
            return false
        }
        let canonical = Data("1|\(code)|\(ticket.plan)|\(ticket.days)|\(ticket.issued)|\(ticket.nonce)".utf8)
        return publicKey.isValidSignature(signature, for: canonical)
    }

    public static func rawSignature(fromDER der: Data) throws -> Data {
        try P256.Signing.ECDSASignature(derRepresentation: der).rawRepresentation
    }

    public static func derSignature(fromRaw raw: Data) throws -> Data {
        try P256.Signing.ECDSASignature(rawRepresentation: raw).derRepresentation
    }

    private static func decodeSignature(_ data: Data) -> P256.Signing.ECDSASignature? {
        if data.count == 64 {
            return try? P256.Signing.ECDSASignature(rawRepresentation: data)
        }
        return try? P256.Signing.ECDSASignature(derRepresentation: data)
    }
}

@MainActor
public final class LicenseStore: ObservableObject {
    @Published public private(set) var statusText: String
    @Published public private(set) var isActive: Bool
    @Published public private(set) var busy: Bool

    private static let stateKey = "license.state.v1"
    private static let deviceIDKey = "license.device-id.v1"
    private static let offlineGraceMilliseconds: Int64 = 24 * 60 * 60 * 1_000
    private var stored: StoredLicense?

    public init() {
        if let data = SecureKeychain.read(account: Self.stateKey),
           let value = try? JSONDecoder().decode(StoredLicense.self, from: data),
           LicenseTicketVerifier.verify(value.ticket, expectedCode: value.ticket.code) {
            stored = value
            if Self.offlineAccessAllowed(value, nowMilliseconds: Self.nowMilliseconds()) {
                isActive = true
                statusText = "已激活（离线验证有效）"
            } else {
                isActive = false
                statusText = "请联网验证授权"
            }
        } else {
            stored = nil
            isActive = false
            statusText = "未激活"
            SecureKeychain.delete(account: Self.stateKey)
        }
        busy = false
    }

    public func activate(code rawCode: String, client: BackendClient) async {
        let code = rawCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        guard Self.validCode(code) else {
            statusText = "卡密格式不正确。"
            return
        }
        guard !busy else { return }
        busy = true
        defer { busy = false }

        do {
            let deviceID = try stableDeviceID()
            let current = stored?.ticket.code
            let action: LicenseAction = current != nil && current != code ? .renew : .activate
            let response = try await client.postLicense(
                action: action,
                code: code,
                currentCode: action == .renew ? current : nil,
                deviceID: deviceID
            )
            if response.ret == 0 {
                // A renewal returns a freshly signed ticket for the existing
                // canonical licenseCode, while the submitted code is consumed.
                let expectedLicenseCode = action == .renew ? current : code
                guard accept(response, expectedLicenseCode: expectedLicenseCode) else {
                    preserveStoredLicenseAfterActivationFailure("授权凭据验证失败。")
                    return
                }
                statusText = "激活成功"
            } else {
                // A bad new/renewal card must never erase a still-valid ticket.
                preserveStoredLicenseAfterActivationFailure(
                    response.message ?? "激活失败（\(response.ret)），请检查卡密后重试。"
                )
            }
        } catch {
            applyOfflineStatus(message: BackendError.wrapping(error).localizedDescription)
        }
    }

    public func verify(client: BackendClient) async {
        guard let current = stored?.ticket.code else {
            isActive = false
            statusText = "未激活"
            return
        }
        guard !busy else { return }
        busy = true
        defer { busy = false }

        do {
            let deviceID = try stableDeviceID()
            let response = try await client.postLicense(
                action: .verify,
                code: current,
                deviceID: deviceID
            )
            if response.ret == 0 {
                guard accept(response, expectedLicenseCode: current) else {
                    applyOfflineStatus(message: "授权凭据验证失败，暂未更新本地授权。")
                    return
                }
                statusText = "授权已验证"
            } else if response.ret == 405 {
                markExpired(response)
            } else if Self.isLicenseRejection(response.ret) {
                clearLicense()
                statusText = response.message ?? "授权已失效（\(response.ret)）。"
            } else {
                applyOfflineStatus(message: response.message)
            }
        } catch {
            applyOfflineStatus(message: BackendError.wrapping(error).localizedDescription)
        }
    }

    public func deviceID() throws -> String {
        try stableDeviceID()
    }

    /// Re-evaluates the local signed ticket and time limits without network access.
    /// The UI should call this periodically and on foreground transitions.
    public func refreshAccess() {
        guard let stored else {
            isActive = false
            if statusText.isEmpty { statusText = "未激活" }
            return
        }
        let wasActive = isActive
        if Self.offlineAccessAllowed(stored, nowMilliseconds: Self.nowMilliseconds()) {
            isActive = true
            if !busy && !wasActive {
                statusText = "已激活（离线验证有效）"
            }
        } else {
            isActive = false
            statusText = stored.expireAt.map { stored.serverNow >= $0 ? "会员已到期，请使用新卡续费。" : "授权离线验证已过期，请联网验证。" }
                ?? "授权离线验证已过期，请联网验证。"
        }
    }

    private func accept(_ response: LicenseEndpointResponse, expectedLicenseCode: String?) -> Bool {
        guard let ticket = response.ticket,
              let licenseCode = response.licenseCode,
              let serverNow = response.serverNow,
              let activatedAt = response.activatedAt,
              LicenseTicketVerifier.verify(ticket, expectedCode: licenseCode),
              expectedLicenseCode.map({ $0.uppercased() == licenseCode.uppercased() }) ?? true else {
            return false
        }
        if let expiry = response.expireAt, serverNow >= expiry {
            return false
        }
        let value = StoredLicense(
            ticket: ticket,
            activatedAt: activatedAt,
            expireAt: response.expireAt,
            serverNow: serverNow,
            lastVerifiedLocal: Self.nowMilliseconds()
        )
        do {
            let data = try JSONEncoder().encode(value)
            try SecureKeychain.write(data, account: Self.stateKey)
        } catch {
            return false
        }
        stored = value
        isActive = true
        return true
    }

    private func applyOfflineStatus(message: String?) {
        if let stored, Self.offlineAccessAllowed(stored, nowMilliseconds: Self.nowMilliseconds()) {
            isActive = true
            statusText = message.map { "暂时无法联网验证，离线授权仍有效。 \($0)" } ?? "暂时无法联网验证，离线授权仍有效。"
        } else {
            isActive = false
            statusText = message ?? "授权验证失败，请联网重试。"
        }
    }

    private func preserveStoredLicenseAfterActivationFailure(_ message: String) {
        if let stored, Self.offlineAccessAllowed(stored, nowMilliseconds: Self.nowMilliseconds()) {
            isActive = true
            statusText = "\(message) 当前授权仍有效。"
        } else {
            isActive = false
            statusText = message
        }
    }

    private func clearLicense() {
        stored = nil
        isActive = false
        SecureKeychain.delete(account: Self.stateKey)
    }

    private func markExpired(_ response: LicenseEndpointResponse) {
        guard var value = stored else {
            isActive = false
            statusText = response.message ?? "会员已到期，请使用新卡续费。"
            return
        }
        let localNow = Self.nowMilliseconds()
        let elapsed = max(0, localNow - value.lastVerifiedLocal)
        let effectiveServerNow = response.serverNow ?? (value.serverNow + elapsed)
        value.expireAt = min(response.expireAt ?? effectiveServerNow, effectiveServerNow)
        if let serverNow = response.serverNow {
            value.serverNow = serverNow
            value.lastVerifiedLocal = localNow
        }
        if let data = try? JSONEncoder().encode(value) {
            try? SecureKeychain.write(data, account: Self.stateKey)
        }
        stored = value
        isActive = false
        statusText = "会员已到期，请使用新卡续费。"
    }

    private func stableDeviceID() throws -> String {
        if let data = SecureKeychain.read(account: Self.deviceIDKey),
           let value = String(data: data, encoding: .utf8),
           value.count == 64,
           value.allSatisfy({ $0.isHexDigit }) {
            return value.lowercased()
        }
        var bytes = Data(count: 32)
        let status = bytes.withUnsafeMutableBytes { buffer in
            SecRandomCopyBytes(kSecRandomDefault, buffer.count, buffer.baseAddress!)
        }
        guard status == errSecSuccess else { throw LicenseStoreError.secureStorage }
        let value = bytes.map { String(format: "%02x", $0) }.joined()
        try SecureKeychain.write(Data(value.utf8), account: Self.deviceIDKey)
        return value
    }

    private static func validCode(_ code: String) -> Bool {
        let parts = code.split(separator: "-", omittingEmptySubsequences: false)
        let alphabet = Set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
        return parts.count == 3
            && parts[0] == "OTV"
            && parts[1].count == 5
            && parts[2].count == 5
            && parts[1].allSatisfy { alphabet.contains($0) }
            && parts[2].allSatisfy { alphabet.contains($0) }
    }

    static func isLicenseRejection(_ ret: Int) -> Bool {
        // These are the backend's explicit verify outcomes for a current card.
        [403, 404, 406].contains(ret)
    }

    private static func offlineAccessAllowed(
        _ value: StoredLicense,
        nowMilliseconds: Int64
    ) -> Bool {
        offlineAccessAllowed(
            ticket: value.ticket,
            expireAt: value.expireAt,
            serverNow: value.serverNow,
            lastVerifiedLocal: value.lastVerifiedLocal,
            nowMilliseconds: nowMilliseconds
        )
    }

    static func offlineAccessAllowed(
        ticket: LicenseTicket,
        expireAt: Int64?,
        serverNow: Int64,
        lastVerifiedLocal: Int64,
        nowMilliseconds: Int64
    ) -> Bool {
        guard LicenseTicketVerifier.verify(ticket, expectedCode: ticket.code) else { return false }
        return offlineTimeAllows(
            expireAt: expireAt,
            serverNow: serverNow,
            lastVerifiedLocal: lastVerifiedLocal,
            nowMilliseconds: nowMilliseconds
        )
    }

    static func offlineTimeAllows(
        expireAt: Int64?,
        serverNow: Int64,
        lastVerifiedLocal: Int64,
        nowMilliseconds: Int64
    ) -> Bool {
        let elapsed = nowMilliseconds - lastVerifiedLocal
        guard elapsed >= 0, elapsed <= offlineGraceMilliseconds else { return false }
        let effectiveServerNow = serverNow + elapsed
        return expireAt.map { effectiveServerNow < $0 } ?? true
    }

    private static func nowMilliseconds() -> Int64 {
        Int64(Date().timeIntervalSince1970 * 1_000)
    }
}

private struct StoredLicense: Codable {
    var ticket: LicenseTicket
    var activatedAt: Int64
    var expireAt: Int64?
    var serverNow: Int64
    var lastVerifiedLocal: Int64
}

private enum LicenseStoreError: Error {
    case secureStorage
}

private enum SecureKeychain {
    private static let service = Bundle.main.bundleIdentifier ?? "com.mytv.ios"

    static func read(account: String) -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess else { return nil }
        return result as? Data
    }

    static func write(_ data: Data, account: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
        let update: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        ]
        let status = SecItemUpdate(query as CFDictionary, update as CFDictionary)
        if status == errSecSuccess { return }
        guard status == errSecItemNotFound else { throw LicenseStoreError.secureStorage }
        var add = query
        update.forEach { add[$0.key] = $0.value }
        guard SecItemAdd(add as CFDictionary, nil) == errSecSuccess else {
            throw LicenseStoreError.secureStorage
        }
    }

    static func delete(account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
        SecItemDelete(query as CFDictionary)
    }
}
