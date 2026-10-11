import Foundation

public struct MediaItem: Identifiable, Codable, Hashable {
    public var id: String
    public var title: String
    public var poster: String
    public var detailRef: String
    public var rating: String
    public var year: String

    public init(id: String, title: String, poster: String = "", detailRef: String = "", rating: String = "", year: String = "") {
        self.id = id
        self.title = title
        self.poster = poster
        self.detailRef = detailRef
        self.rating = rating
        self.year = year
    }
}

public struct MediaSection: Identifiable, Codable, Hashable {
    public var id: String
    public var title: String
    public var items: [MediaItem]

    public init(id: String, title: String, items: [MediaItem]) {
        self.id = id
        self.title = title
        self.items = items
    }
}

public struct MediaSearchPage: Hashable {
    public var items: [MediaItem]
    public var page: Int
    public var hasMore: Bool

    public init(items: [MediaItem], page: Int, hasMore: Bool) {
        self.items = items
        self.page = page
        self.hasMore = hasMore
    }
}

public struct MediaDetail: Codable, Hashable {
    public var item: MediaItem
    public var summary: String
    public var sources: [MediaSource]

    public init(item: MediaItem, summary: String, sources: [MediaSource]) {
        self.item = item
        self.summary = summary
        self.sources = sources
    }
}

public struct MediaSource: Identifiable, Codable, Hashable {
    public var id: String
    public var name: String
    public var episodes: [Episode]

    public init(id: String, name: String, episodes: [Episode]) {
        self.id = id
        self.name = name
        self.episodes = episodes
    }
}

enum PlaybackSourceSelection {
    static func refreshedSource(
        matching selectedSource: MediaSource,
        in refreshedSources: [MediaSource]
    ) -> MediaSource? {
        let idMatches = refreshedSources.filter { $0.id == selectedSource.id }
        if !idMatches.isEmpty {
            return idMatches.count == 1 ? idMatches[0] : nil
        }
        let selectedName = normalized(selectedSource.name)
        guard !selectedName.isEmpty else { return nil }
        let nameMatches = refreshedSources.filter { normalized($0.name) == selectedName }
        return nameMatches.count == 1 ? nameMatches[0] : nil
    }

    static func refreshedEpisode(
        in source: MediaSource,
        matching selectedEpisode: Episode
    ) -> Episode? {
        let selectedName = normalized(selectedEpisode.name)
        if !selectedName.isEmpty {
            let nameMatches = source.episodes.filter { normalized($0.name) == selectedName }
            if nameMatches.count == 1 { return nameMatches[0] }
            if nameMatches.count > 1 { return nil }
        }

        let hasCredibleNames = source.episodes.contains { !normalized($0.name).isEmpty }
        guard selectedName.isEmpty || !hasCredibleNames else { return nil }
        let idMatches = source.episodes.filter { $0.id == selectedEpisode.id }
        return idMatches.count == 1 ? idMatches[0] : nil
    }

    static func episode(
        in source: MediaSource,
        matching currentEpisode: Episode?,
        from currentSource: MediaSource?
    ) -> Episode? {
        guard !source.episodes.isEmpty else { return nil }
        guard let currentEpisode else { return source.episodes.first }
        let episodeName = normalized(currentEpisode.name)
        if let currentSource, currentSource.id != source.id {
            if !episodeName.isEmpty {
                let nameMatches = source.episodes.filter { normalized($0.name) == episodeName }
                if nameMatches.count == 1 { return nameMatches[0] }
            }
            guard let oldIndex = currentSource.episodes.firstIndex(of: currentEpisode) else { return nil }
            return source.episodes[min(oldIndex, source.episodes.count - 1)]
        }

        let idMatches = source.episodes.filter { $0.id == currentEpisode.id }
        if idMatches.count == 1 { return idMatches[0] }
        if !episodeName.isEmpty {
            let nameMatches = source.episodes.filter { normalized($0.name) == episodeName }
            if nameMatches.count == 1 { return nameMatches[0] }
        }
        return nil
    }

    private static func normalized(_ value: String) -> String {
        value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }
}

public struct Episode: Identifiable, Codable, Hashable {
    public var id: String
    public var name: String

    public init(id: String, name: String) {
        self.id = id
        self.name = name
    }
}

public struct TVChannel: Identifiable, Codable, Hashable {
    public var id: String
    public var name: String
    public var group: String
    public var urls: [String]

    public init(id: String, name: String, group: String, urls: [String]) {
        self.id = id
        self.name = name
        self.group = group
        self.urls = urls
    }
}

public struct FootballStreamReference: Identifiable, Codable, Hashable {
    public var id: String
    public var src: String
    public var name: String
    public var player: String
    public var url: URL
    public var referer: String

    public init(id: String, src: String, name: String, player: String, url: URL, referer: String = "") {
        self.id = id
        self.src = src
        self.name = name
        self.player = player
        self.url = url
        self.referer = referer
    }
}

public struct FootballMatch: Identifiable, Codable, Hashable {
    public var id: String
    public var title: String
    public var home: String
    public var away: String
    public var league: String
    public var time: String
    public var status: String
    public var score: String
    public var minute: String
    public var kickoffAt: Date?

    public var isFinished: Bool {
        let normalizedStatus = status.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return ["finished", "ended", "close", "ft", "完场", "完赛", "已结束", "已完赛", "赛后"].contains(normalizedStatus)
    }

    public var isClockHeld: Bool {
        let normalizedStatus = status.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return ["postponed", "delayed", "pending", "cancelled", "canceled", "abandoned",
                "延期", "推迟", "待定", "取消", "腰斩"].contains(normalizedStatus)
    }

    public var displayStatus: String {
        switch status.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "live": return "直播中"
        case "upcoming": return "未开赛"
        case "finished", "ended", "close", "ft", "完场", "完赛", "已结束", "已完赛", "赛后": return "完场"
        case "postponed", "delayed", "延期", "推迟": return "延期"
        case "pending", "待定": return "待定"
        case "cancelled", "canceled", "取消": return "取消"
        case "abandoned", "腰斩": return "腰斩"
        default: return status
        }
    }

    public func projected(at now: Date = Date()) -> FootballMatch {
        if isFinished || isClockHeld { return self }
        guard let kickoffAt else { return self }
        let elapsed = Int(now.timeIntervalSince(kickoffAt) / 60)
        if elapsed < 0 {
            guard status.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "live" else { return self }
            var copy = self
            copy.status = "upcoming"
            copy.minute = ""
            return copy
        }
        guard status.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "upcoming" else { return self }
        let extendedTimeMatch = league.range(of: "杯|淘汰|决赛|半决赛|世界杯|欧洲杯|亚洲杯|美洲杯|欧冠|欧联|欧协|世俱杯|友谊赛", options: .regularExpression) != nil
        let finishGrace = extendedTimeMatch ? 210 : 150
        guard elapsed < finishGrace else { return self }
        var copy = self
        copy.status = "live"
        let played = elapsed <= 45 ? elapsed : (elapsed <= 60 ? 45 : elapsed - 15)
        copy.minute = String(max(0, min(90, played)))
        return copy
    }

    public init(
        id: String,
        title: String,
        home: String,
        away: String,
        league: String,
        time: String,
        status: String,
        score: String = "",
        minute: String = "",
        kickoffAt: Date? = nil
    ) {
        self.id = id
        self.title = title
        self.home = home
        self.away = away
        self.league = league
        self.time = time
        self.status = status
        self.score = score
        self.minute = minute
        self.kickoffAt = kickoffAt
    }
}

public struct PlayableStream: Codable, Hashable {
    public var url: URL
    public var directURL: URL?

    public init(url: URL, directURL: URL? = nil) {
        self.url = url
        self.directURL = directURL
    }
}
