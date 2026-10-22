import SwiftUI

@main
@MainActor
struct FootballLiveApp: App {
    @StateObject private var settings = AppSettings()

    var body: some Scene {
        WindowGroup {
            AppRootView()
                .environmentObject(settings)
        }
    }
}
