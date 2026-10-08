import SwiftUI

struct AppRootView: View {
    @EnvironmentObject private var settings: AppSettings
    @EnvironmentObject private var license: LicenseStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var selectedTab = "home"

    var body: some View {
        Group {
            if settings.client == nil {
                NavigationStack {
                    SettingsView(isFirstRun: true)
                }
            } else {
                TabView(selection: $selectedTab) {
                    NavigationStack {
                        HomeView()
                            .installMyTVDestinations()
                    }
                    .tabItem { Label("首页", systemImage: "house.fill") }
                    .tag("home")
                    .accessibilityIdentifier("tab-home")

                    NavigationStack {
                        SearchView()
                            .installMyTVDestinations()
                    }
                    .tabItem { Label("搜索", systemImage: "magnifyingglass") }
                    .tag("search")
                    .accessibilityIdentifier("tab-search")

                    NavigationStack {
                        LiveTVView()
                            .installMyTVDestinations()
                    }
                    .tabItem { Label("电视", systemImage: "dot.radiowaves.left.and.right") }
                    .tag("live")
                    .accessibilityIdentifier("tab-live")

                    NavigationStack {
                        FootballView()
                    }
                    .tabItem { Label("足球", systemImage: "soccerball") }
                    .tag("football")
                    .accessibilityIdentifier("tab-football")

                    NavigationStack {
                        LibraryView()
                            .installMyTVDestinations()
                    }
                    .tabItem { Label("我的", systemImage: "bookmark.fill") }
                    .tag("library")
                    .accessibilityIdentifier("tab-library")

                }
                .tint(MyTVStyle.accent)
                .toolbarBackground(MyTVStyle.surface, for: .tabBar)
                .toolbarBackground(.visible, for: .tabBar)
            }
        }
        .myTVScreen()
        .task(id: settings.originText) {
            license.refreshAccess()
            guard let client = settings.client else { return }
            await license.verify(client: client)
        }
        .task {
            while !Task.isCancelled {
                do {
                    try await Task.sleep(nanoseconds: 60_000_000_000)
                } catch {
                    return
                }
                guard scenePhase == .active else { continue }
                license.refreshAccess()
            }
        }
        .onChange(of: scenePhase) { phase in
            guard phase == .active, let client = settings.client else { return }
            Task {
                license.refreshAccess()
                await license.verify(client: client)
            }
        }
    }
}
