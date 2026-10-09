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
        if let exact = refreshedSources.first(where: { $0.id == selectedSource.id }) {
            return exact
        }
        let selectedName = normalized(selectedSource.name)
        if !selectedName.isEmpty,
           let named = refreshedSources.first(where: { normalized($0.name) == selectedName }) {
            return named
        }
        return nil
    }

    static func episode(
        in source: MediaSource,
        matching currentEpisode: Episode?,
        from currentSource: MediaSource?
    ) -> Episode? {
        guard !source.episodes.isEmpty else { return nil }
        guard let currentEpisode else { return source.episodes.first }
        if let exact = source.episodes.first(where: { $0.id == currentEpisode.id }) {
            return exact
        }
        let episodeName = normalized(currentEpisode.name)
        if !episodeName.isEmpty,
           let named = source.episodes.first(where: { normalized($0.name) == episodeName }) {
            return named
        }
        if let currentSource,
           let oldIndex = currentSource.episodes.firstIndex(of: currentEpisode) {
            return source.episodes[min(oldIndex, source.episodes.count - 1)]
        }
        return source.episodes.first
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
    public var directURL: URL?

    public init(url: URL, directURL: URL? = nil) {
        self.url = url
        self.directURL = directURL
    }
}
