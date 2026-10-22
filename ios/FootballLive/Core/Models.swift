import Foundation

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

    public init(url: URL) {
        self.url = url
    }
}

struct PlaybackRoute: Hashable, Identifiable {
    let id: String
    let title: String
    let match: FootballMatch
    let stream: FootballStreamReference

    init(footballMatch: FootballMatch, stream: FootballStreamReference) {
        self.id = "football-\(footballMatch.id)-\(stream.id)"
        self.title = "\(footballMatch.home) vs \(footballMatch.away)"
        self.match = footballMatch
        self.stream = stream
    }
}
