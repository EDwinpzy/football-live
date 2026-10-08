import SwiftUI

@main
@MainActor
struct MyTVApp: App {
    @StateObject private var settings = AppSettings()
    @StateObject private var license = LicenseStore()
    @StateObject private var library = LibraryStore()

    var body: some Scene {
        WindowGroup {
            #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("--ui-test-fixtures") {
                UITestFixtureRootView()
                    .environmentObject(settings)
                    .environmentObject(license)
                    .environmentObject(library)
            } else {
                AppRootView()
                    .environmentObject(settings)
                    .environmentObject(license)
                    .environmentObject(library)
            }
            #else
            AppRootView()
                .environmentObject(settings)
                .environmentObject(license)
                .environmentObject(library)
            #endif
        }
    }
}
