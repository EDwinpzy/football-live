import CoreFoundation
import Foundation

public enum BackendError: Error, LocalizedError, Equatable {
    case invalidOrigin
    case invalidResponse
    case httpStatus(Int)
    case malformedPayload(String)
    case certificateTrust
    case network(String)

    public var errorDescription: String? {
        switch self {
        case .invalidOrigin:
            return "服务器地址必须是有效的 HTTPS 域名或 IP，且不能包含路径。"
        case .invalidResponse:
            return "服务器没有返回有效响应，请稍后重试。"
        case let .httpStatus(status):
            return "服务器请求失败（HTTP \(status)）。"
        case let .malformedPayload(message):
            return message
        case .certificateTrust:
            return "无法验证服务器证书。请先在 iPhone 设置中安装并完全信任项目提供的公共迁移 CA，然后重试。"
        case let .network(message):
            return message
        }
    }

    static func wrapping(_ error: Error) -> BackendError {
        let nsError = error as NSError
        if nsError.domain == NSURLErrorDomain,
           [-1200, -1201, -1202, -1203, -1204, -1205, -1206].contains(nsError.code) {
            return .certificateTrust
        }
        return .network("连接服务器失败：\(error.localizedDescription)")
    }
}

/// HTTPS API used by the football match list, stream resolver, and relay.
public struct BackendClient {
    public let origin: URL
    private let session: URLSession

    public init(origin: URL, session: URLSession = .shared) {
        self.origin = origin
        self.session = session
    }

    public static let defaultOrigin = URL(string: "https://103.236.97.137:35208")!

    public static func validatedOrigin(_ raw: String) -> URL? {
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let normalized = value.hasSuffix("/") ? String(value.dropLast()) : value
        guard let url = URL(string: normalized), isValidOrigin(url) else { return nil }
        return url
    }

    public static func isValidOrigin(_ url: URL) -> Bool {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == "https",
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil,
              components.path.isEmpty || components.path == "/" else {
            return false
        }
        return true
    }

    public func matches(fresh: Bool = false) async throws -> [FootballMatch] {
        let query = fresh ? [URLQueryItem(name: "fresh", value: "1")] : []
        let data = try await get(segments: ["api", "matches"], query: query)
        let object = try jsonObject(data)
        guard let rawMatches = object["matches"] as? [[String: Any]] else {
            throw BackendError.malformedPayload("比赛列表格式不正确。")
        }
        return rawMatches.compactMap(parseFootballMatch)
    }

    public func matchStreams(match: FootballMatch) async throws -> [FootballStreamReference] {
        let data = try await get(
            segments: ["api", "stream", match.id],
            query: [
                URLQueryItem(name: "src", value: "all"),
                URLQueryItem(name: "fresh", value: "1")
            ]
        )
        let object = try jsonObject(data)
        guard let rawSources = object["sources"] as? [[String: Any]] else {
            throw BackendError.malformedPayload("直播线路列表格式不正确。")
        }
        var seenSources = Set<String>()
        return rawSources.compactMap { raw -> FootballStreamReference? in
            let source = Self.string(raw["source"]).trimmingCharacters(in: .whitespacesAndNewlines)
            let player = Self.string(raw["player"]).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            let rawURL = Self.string(raw["url"]).trimmingCharacters(in: .whitespacesAndNewlines)
            guard !source.isEmpty,
                  seenSources.insert(source).inserted,
                  player == "m3u8",
                  let streamURL = Self.validStreamURL(rawURL) else { return nil }
            let name = Self.string(raw["name"]).trimmingCharacters(in: .whitespacesAndNewlines)
            let referer = Self.string(raw["referer"]).trimmingCharacters(in: .whitespacesAndNewlines)
            return FootballStreamReference(
                id: source,
                src: source,
                name: name.isEmpty ? source : name,
                player: player,
                url: streamURL,
                referer: referer
            )
        }
    }

    public func matchStream(match: FootballMatch, source: FootballStreamReference) async throws -> PlayableStream {
        guard source.player.lowercased() == "m3u8" else {
            throw BackendError.malformedPayload("所选直播线路不是原生 HLS。")
        }
        let data = try await get(
            segments: ["api", "stream", match.id],
            query: [
                URLQueryItem(name: "src", value: source.src),
                URLQueryItem(name: "fresh", value: "1")
            ]
        )
        let object = try jsonObject(data)
        let responseSource = Self.string(object["source"]).trimmingCharacters(in: .whitespacesAndNewlines)
        guard responseSource.isEmpty || responseSource == source.src else {
            throw BackendError.malformedPayload("刷新后的直播线路与所选线路不一致。")
        }
        let matchingSource = (object["sources"] as? [[String: Any]])?.first {
            Self.string($0["source"]).trimmingCharacters(in: .whitespacesAndNewlines) == source.src
        }
        let matchingPlayer = Self.string(matchingSource?["player"]).trimmingCharacters(in: .whitespacesAndNewlines)
        guard matchingPlayer.isEmpty || matchingPlayer.lowercased() == "m3u8" else {
            throw BackendError.malformedPayload("刷新后的直播线路不再提供原生 HLS。")
        }
        let rawURL = Self.string(object["url"]).ifBlank(computing: {
            Self.string(matchingSource?["url"])
        }).trimmingCharacters(in: .whitespacesAndNewlines)
        guard let streamURL = URL(string: rawURL),
              ["https", "http"].contains(streamURL.scheme?.lowercased() ?? ""),
              streamURL.host?.isEmpty == false else {
            throw BackendError.malformedPayload("此场比赛当前没有可播放信号。")
        }
        let referer = Self.string(object["referer"]).ifBlank(computing: {
            Self.string(matchingSource?["referer"]).ifBlank(source.referer)
        })
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .nilIfBlank
        return PlayableStream(url: relayURL(for: streamURL.absoluteString, ref: referer))
    }

    public func relayURL(for rawURL: String, ref: String? = nil) -> URL {
        guard Self.isValidOrigin(origin),
              let originComponents = URLComponents(url: origin, resolvingAgainstBaseURL: false) else {
            return origin
        }
        if let existing = URL(string: rawURL),
           let components = URLComponents(url: existing, resolvingAgainstBaseURL: false),
           components.scheme?.lowercased() == "https",
           components.host?.lowercased() == origin.host?.lowercased(),
           Self.effectivePort(components) == Self.effectivePort(originComponents),
           components.path == "/web/api/relay",
           components.queryItems?.contains(where: { $0.name == "u" && !($0.value ?? "").isEmpty }) == true {
            return existing
        }
        var query = [URLQueryItem(name: "u", value: rawURL)]
        if let ref { query.append(URLQueryItem(name: "ref", value: ref)) }
        return (try? endpointURL(segments: ["api", "relay"], query: query)) ?? origin
    }

    func endpointURL(segments: [String], query: [URLQueryItem] = [], webRoot: Bool = true) throws -> URL {
        guard Self.isValidOrigin(origin),
              var components = URLComponents(url: origin, resolvingAgainstBaseURL: false) else {
            throw BackendError.invalidOrigin
        }
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:@")
        let encodedSegments = try segments.map { segment -> String in
            guard let encoded = segment.addingPercentEncoding(withAllowedCharacters: allowed) else {
                throw BackendError.invalidOrigin
            }
            return encoded
        }
        components.percentEncodedPath = (webRoot ? "/web" : "") + "/" + encodedSegments.joined(separator: "/")
        components.queryItems = query.isEmpty ? nil : query
        if let encodedQuery = components.percentEncodedQuery {
            components.percentEncodedQuery = encodedQuery.replacingOccurrences(of: "+", with: "%2B")
        }
        guard let url = components.url else { throw BackendError.invalidOrigin }
        return url
    }

    private func get(segments: [String], query: [URLQueryItem], timeout: TimeInterval = 30) async throws -> Data {
        let url = try endpointURL(segments: segments, query: query)
        var request = URLRequest(url: url)
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("FootballLive iOS", forHTTPHeaderField: "User-Agent")
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw BackendError.wrapping(error)
        }
        guard let http = response as? HTTPURLResponse else { throw BackendError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else { throw BackendError.httpStatus(http.statusCode) }
        return data
    }

    private func jsonObject(_ data: Data) throws -> [String: Any] {
        let raw: Any
        do {
            raw = try JSONSerialization.jsonObject(with: data)
        } catch {
            throw BackendError.malformedPayload("服务器返回的内容不是有效 JSON。")
        }
        guard let object = raw as? [String: Any] else {
            throw BackendError.malformedPayload("服务器返回的数据结构不正确。")
        }
        if object["ok"] as? Bool == false, let message = object["error"] as? String {
            throw BackendError.malformedPayload(message)
        }
        return object
    }

    private func parseFootballMatch(_ raw: [String: Any]) -> FootballMatch? {
        let id = Self.string(raw["match_id"])
        let home = Self.string(raw["home"])
        let away = Self.string(raw["away"])
        guard !id.isEmpty, !home.isEmpty, !away.isEmpty else { return nil }
        let date = Self.string(raw["date"])
        let dateFull = Self.string(raw["date_full"])
        let clock = Self.string(raw["time"])
        let time = [date, clock].filter { !$0.isEmpty }.joined(separator: " ")
        let title = Self.string(raw["title"]).ifBlank("\(home) vs \(away)")
        let scoreObject = raw["score"] as? [String: Any] ?? [:]
        let homeScore = Self.string(scoreObject["home"]).ifBlank(Self.string(scoreObject["h"]))
        let awayScore = Self.string(scoreObject["away"]).ifBlank(Self.string(scoreObject["a"]))
        let score = homeScore.isEmpty || awayScore.isEmpty ? "" : "\(homeScore)-\(awayScore)"
        return FootballMatch(
            id: id,
            title: title,
            home: home,
            away: away,
            league: Self.string(raw["league"]),
            time: time,
            status: Self.string(raw["status"]),
            score: score,
            minute: Self.string(raw["minute"]),
            kickoffAt: Self.kickoffDate(dateFull: dateFull, clock: clock)
        )
    }

    private static func kickoffDate(dateFull: String, clock: String) -> Date? {
        guard !dateFull.isEmpty, !clock.isEmpty else { return nil }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        formatter.dateFormat = "yyyy-MM-dd HH:mm"
        return formatter.date(from: "\(dateFull) \(clock)")
    }

    private static func validStreamURL(_ raw: String) -> URL? {
        guard let url = URL(string: raw),
              ["https", "http"].contains(url.scheme?.lowercased() ?? ""),
              url.host?.isEmpty == false else { return nil }
        return url
    }

    private static func effectivePort(_ components: URLComponents) -> Int? {
        components.port ?? (components.scheme?.lowercased() == "https" ? 443 : 80)
    }

    private static func string(_ value: Any?) -> String {
        guard let value, !(value is NSNull) else { return "" }
        if let string = value as? String { return string }
        if let number = value as? NSNumber {
            if CFGetTypeID(number) == CFBooleanGetTypeID() { return number.boolValue ? "true" : "false" }
            return number.stringValue
        }
        return ""
    }
}

private extension String {
    func ifBlank(_ fallback: @autoclosure () -> String) -> String {
        trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? fallback() : self
    }

    func ifBlank(computing fallback: () -> String) -> String {
        trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? fallback() : self
    }

    var nilIfBlank: String? {
        trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : self
    }
}
