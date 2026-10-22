import SwiftUI

struct AppRootView: View {
    @EnvironmentObject private var settings: AppSettings

    var body: some View {
        Group {
            if settings.client == nil {
                NavigationStack {
                    SettingsView(isFirstRun: true)
                }
            } else {
                NavigationStack {
                    FootballView()
                }
                .tint(FootballLiveStyle.accent)
            }
        }
        .footballLiveScreen()
    }
}
