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
            return "无法验证服务器证书。请先在 iPhone 设置中安装并完全信任 My TV 根证书，然后重试。"
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

enum LicenseAction: String {
    case activate
    case verify
    case renew
    case restore
}

struct LicenseEndpointResponse {
    var ret: Int
    var message: String?
    var ticket: LicenseTicket?
    var licenseCode: String?
    var activatedAt: Int64?
    var expireAt: Int64?
    var serverNow: Int64?
}

/// Public HTTPS API client. Content routes use the web mount; licensing uses the origin root.
public struct BackendClient {
    public let origin: URL
    private let session: URLSession

    public init(origin: URL, session: URLSession = .shared) {
        self.origin = origin
        self.session = session
    }

    public static let defaultOrigin = URL(string: "https://103.236.97.137:35208")!
    public static let supportedCategories: Set<String> = ["movie", "tv", "anime", "variety", "short"]

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

    public func home(category: String) async throws -> [MediaSection] {
        guard Self.supportedCategories.contains(category) else {
            throw BackendError.malformedPayload("不支持的影视分类。")
        }
        let data = try await get(
            segments: ["vod", "home"],
            query: [URLQueryItem(name: "category", value: category)]
        )
        let object = try jsonObject(data)
        guard let rawSections = object["sections"] as? [[String: Any]] else {
            throw BackendError.malformedPayload("首页数据格式不正确。")
        }
        return rawSections.enumerated().compactMap { index, section in
            let title = Self.string(section["title"]).trimmingCharacters(in: .whitespacesAndNewlines)
            guard !title.isEmpty else { return nil }
            let items = (section["items"] as? [[String: Any]] ?? []).compactMap {
                parseMediaItem($0, kind: .douban)
            }
            return MediaSection(
                id: Self.string(section["id"]).ifBlank("\(category)-\(index)"),
                title: title,
                items: items
            )
        }
    }

    public func search(query: String, page: Int = 1) async throws -> [MediaItem] {
        let data = try await get(
            segments: ["vod", "search"],
            query: [
                URLQueryItem(name: "q", value: query),
                URLQueryItem(name: "page", value: String(max(1, page)))
            ]
        )
        let object = try jsonObject(data)
        guard let items = object["items"] as? [[String: Any]] else {
            throw BackendError.malformedPayload("搜索结果格式不正确。")
        }
        return items.compactMap { parseMediaItem($0, kind: .douban) }
    }

    public func detail(item: MediaItem) async throws -> MediaDetail {
        let route = Self.detailRoute(detailRef: item.detailRef, itemID: item.id)
        let data = try await get(segments: route.segments, query: [])
        let object = try jsonObject(data)
        let kind: MediaKind = route.isHHKan ? .hhkan : .douban
        var detailItem = parseMediaItem(object, kind: kind) ?? item
        detailItem.detailRef = route.isHHKan ? "hhkan:\(route.contentID)" : route.contentID
        if detailItem.title.isEmpty { detailItem.title = item.title }
        if detailItem.poster.isEmpty { detailItem.poster = item.poster }
        if detailItem.rating.isEmpty { detailItem.rating = item.rating }
        if detailItem.year.isEmpty { detailItem.year = item.year }
        let summary = Self.string(object[route.isHHKan ? "desc" : "summary"])
        let sources = (object["sources"] as? [[String: Any]] ?? []).enumerated().map { index, source in
            parseSource(source, index: index, kind: kind)
        }
        return MediaDetail(item: detailItem, summary: summary, sources: sources)
    }

    public func play(detailRef: String, source: MediaSource, episode: Episode) async throws -> PlayableStream {
        let route = Self.contentRoute(detailRef: detailRef)
        let segments = route.isHHKan
            ? ["hhkan", "play", route.contentID, source.id, episode.id]
            : ["vod", "play", route.contentID, source.id, episode.id]
        let data = try await get(segments: segments, query: [])
        let object = try jsonObject(data)
        let firstSource = (object["sources"] as? [[String: Any]])?.compactMap {
            Self.string($0["url"]).nilIfBlank
        }.first ?? ""
        let rawURL = Self.string(object["url"]).ifBlank(computing: {
            Self.string(object["m3u8"]).ifBlank(firstSource)
        })
        guard let streamURL = URL(string: rawURL),
              ["https", "http"].contains(streamURL.scheme?.lowercased() ?? "") else {
            throw BackendError.malformedPayload("服务器没有返回可播放地址。")
        }
        let directURL = Self.isDirectPlaybackURL(streamURL) ? streamURL : nil
        return PlayableStream(
            url: relayURL(for: streamURL.absoluteString, ref: "none"),
            directURL: directURL
        )
    }

    public func channels() async throws -> [TVChannel] {
        let data = try await get(segments: ["api", "iptv"], query: [])
        let object = try jsonObject(data)
        guard let groups = object["groups"] as? [[String: Any]] else {
            throw BackendError.malformedPayload("电视频道列表格式不正确。")
        }
        return groups.flatMap { group -> [TVChannel] in
            let groupName = Self.string(group["name"]).ifBlank("其他")
            return (group["channels"] as? [[String: Any]] ?? []).compactMap { channel -> TVChannel? in
                let name = Self.string(channel["name"]).trimmingCharacters(in: .whitespacesAndNewlines)
                let urls = (channel["urls"] as? [Any] ?? []).map(Self.string).filter { !$0.isEmpty }
                guard !name.isEmpty, !urls.isEmpty else { return nil }
                let stableID = [groupName, name, urls.first ?? ""].joined(separator: "|")
                return TVChannel(id: stableID, name: name, group: groupName, urls: urls)
            }
        }
    }

    public func matches() async throws -> [FootballMatch] {
        let data = try await get(segments: ["api", "matches"], query: [])
        let object = try jsonObject(data)
        guard let rawMatches = object["matches"] as? [[String: Any]] else {
            throw BackendError.malformedPayload("比赛列表格式不正确。")
        }
        return rawMatches.compactMap(parseFootballMatch)
    }

    public func matchStream(match: FootballMatch, sourceIndex: Int) async throws -> PlayableStream {
        guard match.streams.indices.contains(sourceIndex) else {
            throw BackendError.malformedPayload("所选直播线路不存在。")
        }
        let source = match.streams[sourceIndex]
        let data = try await get(
            // The upstream's channel id can differ from match_id; the browser
            // client requests /api/stream/{channel.id}?src=... for each line.
            segments: ["api", "stream", source.id],
            query: [URLQueryItem(name: "src", value: source.src)]
        )
        let object = try jsonObject(data)
        let firstSource = (object["sources"] as? [[String: Any]])?.compactMap {
            Self.string($0["url"]).nilIfBlank
        }.first ?? ""
        let rawURL = Self.string(object["url"]).ifBlank(computing: {
            Self.string(object["m3u8"]).ifBlank(firstSource)
        })
        guard let streamURL = URL(string: rawURL),
              ["https", "http"].contains(streamURL.scheme?.lowercased() ?? "") else {
            throw BackendError.malformedPayload("此场比赛当前没有可播放信号。")
        }
        return PlayableStream(url: relayURL(for: streamURL.absoluteString))
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
        return (try? endpointURL(
            segments: ["api", "relay"],
            query: query
        )) ?? origin
    }

    public func imageURL(for rawURL: String) -> URL? {
        let value = rawURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty,
              let candidate = URL(string: value, relativeTo: origin)?.absoluteURL else {
            return nil
        }
        let host = candidate.host?.lowercased() ?? ""
        if host.hasSuffix("doubanio.com") || candidate.scheme?.lowercased() == "http" {
            return try? endpointURL(
                segments: ["hhkan", "proxy"],
                query: [
                    URLQueryItem(name: "u", value: candidate.absoluteString),
                    URLQueryItem(name: "poster_cache", value: "2")
                ]
            )
        }
        guard candidate.scheme?.lowercased() == "https" else { return nil }
        return candidate
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

    private static func effectivePort(_ components: URLComponents) -> Int? {
        components.port ?? (components.scheme?.lowercased() == "https" ? 443 : 80)
    }

    private static func isDirectPlaybackURL(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "https",
              let host = url.host?.lowercased() else { return false }
        return host != "localhost"
            && host != "::1"
            && !host.hasPrefix("127.")
    }

    func postLicense(
        action: LicenseAction,
        code: String,
        currentCode: String? = nil,
        deviceID: String
    ) async throws -> LicenseEndpointResponse {
        var payload: [String: Any] = [
            "protocol": 2,
            "action": action.rawValue,
            "code": code,
            "deviceId": deviceID
        ]
        if action == .renew, let currentCode { payload["currentCode"] = currentCode }
        let body = try JSONSerialization.data(withJSONObject: payload)
        let url = try endpointURL(segments: ["activate"], webRoot: false)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = body

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw BackendError.wrapping(error)
        }
        guard let http = response as? HTTPURLResponse else { throw BackendError.invalidResponse }
        guard let object = try? jsonObject(data) else {
            throw BackendError.malformedPayload("授权服务器返回的数据无法读取。")
        }
        return LicenseEndpointResponse(
            ret: Self.integer(object["ret"]) ?? http.statusCode,
            message: Self.string(object["msg"]).nilIfBlank,
            ticket: (object["ticket"] as? [String: Any]).flatMap(LicenseTicket.init(json:)),
            licenseCode: Self.string(object["licenseCode"]).nilIfBlank,
            activatedAt: Self.timestampMilliseconds(object["activatedAt"]),
            expireAt: Self.timestampMilliseconds(object["expireAt"]),
            serverNow: Self.timestampMilliseconds(object["serverNow"])
        )
    }

    static func detailRoute(detailRef: String, itemID: String) -> (isHHKan: Bool, contentID: String, segments: [String]) {
        let route = contentRoute(detailRef: detailRef, itemID: itemID)
        if route.isHHKan {
            return (true, route.contentID, ["hhkan", "detail", route.contentID])
        }
        return (false, route.contentID, ["vod", "detail", route.contentID])
    }

    static func contentRoute(detailRef: String, itemID: String = "") -> (isHHKan: Bool, contentID: String) {
        let ref = detailRef.trimmingCharacters(in: .whitespacesAndNewlines)
        if ref.hasPrefix("hhkan:") {
            return (true, String(ref.dropFirst("hhkan:".count)))
        }
        if itemID.hasPrefix("hhkan:") {
            return (true, String(itemID.dropFirst("hhkan:".count)))
        }
        let fallback = ref.isEmpty ? itemID : ref
        let contentID = fallback.hasPrefix("douban:") ? String(fallback.dropFirst("douban:".count)) : fallback
        return (false, contentID)
    }

    private enum MediaKind { case douban, hhkan }

    private func get(segments: [String], query: [URLQueryItem]) async throws -> Data {
        let url = try endpointURL(segments: segments, query: query)
        var request = URLRequest(url: url)
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("MyTV iOS", forHTTPHeaderField: "User-Agent")
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

    private func parseMediaItem(_ raw: [String: Any], kind: MediaKind) -> MediaItem? {
        let rawID = Self.string(raw["id"]).ifBlank(computing: { Self.string(raw["douban_id"]) })
        guard !rawID.isEmpty else { return nil }
        let contentID: String
        let stableID: String
        let detailRef: String
        switch kind {
        case .douban:
            contentID = rawID.hasPrefix("douban:") ? String(rawID.dropFirst("douban:".count)) : rawID
            stableID = "douban:\(contentID)"
            detailRef = contentID
        case .hhkan:
            contentID = rawID.hasPrefix("hhkan:") ? String(rawID.dropFirst("hhkan:".count)) : rawID
            stableID = "hhkan:\(contentID)"
            detailRef = stableID
        }
        let rawPoster = Self.string(raw["poster_url"]).ifBlank(computing: { Self.string(raw["cover"]) })
        let poster = imageURL(for: rawPoster)?.absoluteString ?? rawPoster
        let releaseDate = Self.string(raw["release_date"])
        return MediaItem(
            id: stableID,
            title: Self.string(raw["title"]).trimmingCharacters(in: .whitespacesAndNewlines),
            poster: poster,
            detailRef: detailRef,
            rating: Self.decimalString(raw[kind == .hhkan ? "score" : "rating"]),
            year: Self.string(raw["year"]).ifBlank(computing: { String(releaseDate.prefix(4)) })
        )
    }

    private func parseSource(_ raw: [String: Any], index: Int, kind: MediaKind) -> MediaSource {
        let rawEpisodes = raw["episodes"] as? [[String: Any]] ?? []
        let episodes = rawEpisodes.enumerated().map { episodeIndex, episode in
            let episodeName = Self.string(episode["name"]).ifBlank(computing: { Self.string(episode["ep"]) })
            let name = episodeName.ifBlank("第\(episodeIndex + 1)集")
            let episodeID: String
            if kind == .hhkan {
                episodeID = Self.string(episode["vid"]).ifBlank(String(episodeIndex))
            } else {
                episodeID = String(episodeIndex)
            }
            return Episode(id: episodeID, name: name)
        }
        let sourceID = Self.string(raw["id"]).ifBlank(computing: {
            let key = kind == .hhkan ? "pid" : "id"
            return Self.string(rawEpisodes.first?[key]).ifBlank(String(index))
        })
        return MediaSource(
            id: sourceID,
            name: Self.string(raw["name"]).ifBlank("线路\(index + 1)"),
            episodes: episodes
        )
    }

    private func parseFootballMatch(_ raw: [String: Any]) -> FootballMatch? {
        let id = Self.string(raw["match_id"])
        let home = Self.string(raw["home"])
        let away = Self.string(raw["away"])
        guard !id.isEmpty, !home.isEmpty, !away.isEmpty else { return nil }
        let streams = (raw["channels"] as? [[String: Any]] ?? []).compactMap { channel -> FootballStreamReference? in
            let source = Self.string(channel["src"])
            guard !source.isEmpty else { return nil }
            return FootballStreamReference(
                id: Self.string(channel["id"]).ifBlank(id),
                src: source,
                name: Self.string(channel["name"]).ifBlank(source)
            )
        }
        let date = Self.string(raw["date"])
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
            streams: streams
        )
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

    private static func integer(_ value: Any?) -> Int? {
        guard let value, !(value is NSNull) else { return nil }
        if let number = value as? NSNumber { return number.intValue }
        return Int(value as? String ?? "")
    }

    private static func decimalString(_ value: Any?) -> String {
        guard let value, !(value is NSNull) else { return "" }
        if let string = value as? String { return string }
        if let number = value as? NSNumber {
            return number.doubleValue.rounded() == number.doubleValue
                ? String(number.intValue)
                : String(format: "%.1f", number.doubleValue)
        }
        return ""
    }

    static func timestampMilliseconds(_ value: Any?) -> Int64? {
        guard let value, !(value is NSNull) else { return nil }
        if let number = value as? NSNumber {
            return milliseconds(from: number.doubleValue)
        }
        guard let string = value as? String else { return nil }
        if let number = Double(string) { return milliseconds(from: number) }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = fractional.date(from: string) ?? ISO8601DateFormatter().date(from: string)
        guard let date else { return nil }
        return Int64(date.timeIntervalSince1970 * 1_000)
    }

    private static func milliseconds(from raw: Double) -> Int64? {
        guard raw.isFinite, raw > 0 else { return nil }
        let value = raw < 1_000_000_000_000 ? raw * 1_000 : raw
        guard value <= Double(Int64.max) else { return nil }
        return Int64(value)
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
