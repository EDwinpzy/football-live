import Combine
import Foundation

@MainActor
public final class LibraryStore: ObservableObject {
    @Published public private(set) var favorites: [MediaItem] {
        didSet { persist(favorites, key: Self.favoritesKey) }
    }

    @Published public private(set) var history: [MediaItem] {
        didSet { persist(history, key: Self.historyKey) }
    }

    private let defaults: UserDefaults
    private static let favoritesKey = "mytv.library.favorites.v1"
    private static let historyKey = "mytv.library.history.v1"
    private let historyLimit = 100

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        favorites = Self.loadItems(from: defaults, key: Self.favoritesKey)
        history = Self.loadItems(from: defaults, key: Self.historyKey)
    }

    public func isFavorite(_ item: MediaItem) -> Bool {
        favorites.contains { $0.id == item.id }
    }

    public func toggleFavorite(_ item: MediaItem) {
        if let index = favorites.firstIndex(where: { $0.id == item.id }) {
            favorites.remove(at: index)
        } else {
            favorites.insert(item, at: 0)
        }
    }

    public func recordWatching(item: MediaItem) {
        history.removeAll { $0.id == item.id }
        history.insert(item, at: 0)
        if history.count > historyLimit {
            history = Array(history.prefix(historyLimit))
        }
    }

    private func persist(_ items: [MediaItem], key: String) {
        guard let data = try? JSONEncoder().encode(items) else { return }
        defaults.set(data, forKey: key)
    }

    private static func loadItems(from defaults: UserDefaults, key: String) -> [MediaItem] {
        guard let data = defaults.data(forKey: key) else { return [] }
        return (try? JSONDecoder().decode([MediaItem].self, from: data)) ?? []
    }
}
