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
}
