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

    public init(id: String, src: String, name: String) {
        self.id = id
        self.src = src
        self.name = name
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
    public var streams: [FootballStreamReference]

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
        streams: [FootballStreamReference]
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
        self.streams = streams
    }
}

public struct PlayableStream: Codable, Hashable {
    public var url: URL

    public init(url: URL) {
        self.url = url
    }
}
