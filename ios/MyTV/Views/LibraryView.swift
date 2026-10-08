import SwiftUI

struct LibraryView: View {
    @EnvironmentObject private var library: LibraryStore
    @State private var selection = "favorites"

    private var items: [MediaItem] {
        selection == "favorites" ? library.favorites : library.history
    }

    private let columns = [GridItem(.adaptive(minimum: 132), spacing: 14, alignment: .top)]

    var body: some View {
        VStack(spacing: 0) {
            Picker("片单", selection: $selection) {
                Text("收藏").tag("favorites")
                Text("历史").tag("history")
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 18)
            .padding(.top, 14)
            .accessibilityIdentifier("library-segment")

            if items.isEmpty {
                ScreenStateView(
                    symbol: selection == "favorites" ? "bookmark" : "clock.arrow.circlepath",
                    title: selection == "favorites" ? "还没有收藏" : "暂无观看记录",
                    message: selection == "favorites" ? "打开影片详情，点亮收藏按钮即可保存。" : "开始播放后，影片会出现在这里。"
                )
                .accessibilityIdentifier(selection == "favorites" ? "favorites-empty-state" : "history-empty-state")
            } else {
                ScrollView {
                    LazyVGrid(columns: columns, alignment: .leading, spacing: 20) {
                        ForEach(items) { item in
                            MediaPosterCard(item: item)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(.horizontal, 18)
                    .padding(.vertical, 20)
                }
            }
        }
        .background(MyTVStyle.background)
        .navigationTitle("我的")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .navigationBarTrailing) {
                NavigationLink {
                    SettingsView()
                } label: {
                    Image(systemName: "gearshape.fill")
                        .foregroundStyle(MyTVStyle.secondaryText)
                }
                .accessibilityLabel("设置")
                .accessibilityIdentifier("library-settings-link")
            }
        }
        .accessibilityIdentifier("library-screen")
    }
}
