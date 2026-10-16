import Combine
import Foundation

@MainActor
public final class AppSettings: ObservableObject {
    @Published public var originText: String

    public var client: BackendClient? {
        guard let origin = BackendClient.validatedOrigin(originText) else { return nil }
        return BackendClient(origin: origin)
    }

    private let defaults: UserDefaults
    private static let originKey = "mytv.backend.origin.v1"
    private static let vodSourcePreferencesKey = "mytv.vod.sourcePreferences.v1"

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        if let saved = defaults.string(forKey: Self.originKey),
           BackendClient.validatedOrigin(saved) != nil {
            originText = saved
        } else {
            originText = BackendClient.defaultOrigin.absoluteString
        }
    }

    public func saveOrigin(_ raw: String) throws {
        guard let origin = BackendClient.validatedOrigin(raw) else {
            throw BackendError.invalidOrigin
        }
        let normalized = origin.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        defaults.set(normalized, forKey: Self.originKey)
        originText = normalized
    }

    func rememberedVODSource(for itemID: String) -> PlaybackSourcePreference? {
        guard let data = defaults.data(forKey: Self.vodSourcePreferencesKey),
              let preferences = try? JSONDecoder().decode([String: PlaybackSourcePreference].self, from: data) else {
            return nil
        }
        return preferences[itemID]
    }

    func rememberVODSource(_ source: MediaSource, for itemID: String) {
        var preferences: [String: PlaybackSourcePreference] = [:]
        if let data = defaults.data(forKey: Self.vodSourcePreferencesKey),
           let saved = try? JSONDecoder().decode([String: PlaybackSourcePreference].self, from: data) {
            preferences = saved
        }
        preferences[itemID] = PlaybackSourcePreference(id: source.id, name: source.name)
        guard let data = try? JSONEncoder().encode(preferences) else { return }
        defaults.set(data, forKey: Self.vodSourcePreferencesKey)
    }
}
