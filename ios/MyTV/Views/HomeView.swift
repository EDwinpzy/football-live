import SwiftUI

private struct HomeCategory: Identifiable {
    let id: String
    let title: String

    static let all = [
        HomeCategory(id: "movie", title: "电影"),
        HomeCategory(id: "tv", title: "剧集"),
        HomeCategory(id: "anime", title: "动漫"),
        HomeCategory(id: "variety", title: "综艺")
    ]
}

struct HomeView: View {
    @EnvironmentObject private var settings: AppSettings

    private let fixtureSections: [MediaSection]?
    @State private var selectedCategory = "movie"
    @State private var sections: [MediaSection] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var retryToken = 0

    init(fixtureSections: [MediaSection]? = nil) {
        self.fixtureSections = fixtureSections
    }

    private var visibleSections: [MediaSection] {
        fixtureSections ?? sections
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                header
                categoryPicker

                if let fixtureSections {
                    sectionList(fixtureSections)
                } else if isLoading {
                    loadingState
                } else if let errorMessage {
                    ScreenStateView(
                        symbol: "wifi.exclamationmark",
                        title: "片库加载失败",
                        message: errorMessage,
                        actionTitle: "重试",
                        action: { retryToken += 1 }
                    )
                    .frame(minHeight: 280)
                    .accessibilityIdentifier("home-error-state")
                } else if visibleSections.allSatisfy({ $0.items.isEmpty }) {
                    ScreenStateView(
                        symbol: "film",
                        title: "这里暂时没有内容",
                        message: "稍后再试，或切换到其他分类。"
                    )
                    .frame(minHeight: 280)
                    .accessibilityIdentifier("home-empty-state")
                } else {
                    sectionList(visibleSections)
                }
            }
            .padding(.top, 16)
            .padding(.bottom, 30)
        }
        .background(MyTVStyle.background)
        .toolbar(.hidden, for: .navigationBar)
        .task(id: "\(selectedCategory)-\(retryToken)") {
            guard fixtureSections == nil else { return }
            await loadHome()
        }
        .accessibilityIdentifier("home-screen")
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(alignment: .leading, spacing: 5) {
                Text("My TV")
                    .font(.system(size: 32, weight: .bold, design: .rounded))
                    .foregroundStyle(.white)
                    .accessibilityIdentifier("home-title")
                Text("为你发现想看的内容")
                    .font(.subheadline)
                    .foregroundStyle(MyTVStyle.secondaryText)
            }
            Spacer(minLength: 8)
            Image(systemName: "play.tv.fill")
                .font(.system(size: 25, weight: .medium))
                .foregroundStyle(MyTVStyle.accent)
                .frame(width: 54, height: 54)
                .background(MyTVStyle.surface, in: RoundedRectangle(cornerRadius: 17, style: .continuous))
                .accessibilityHidden(true)
        }
        .padding(.horizontal, 20)
    }

    private var categoryPicker: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 10) {
                ForEach(HomeCategory.all) { category in
                    let selected = selectedCategory == category.id
                    Button {
                        selectedCategory = category.id
                    } label: {
                        Text(category.title)
                            .font(.subheadline.weight(selected ? .semibold : .medium))
                            .foregroundStyle(selected ? .white : MyTVStyle.secondaryText)
                            .padding(.horizontal, 18)
                            .padding(.vertical, 10)
                            .background(selected ? MyTVStyle.accent : MyTVStyle.surface, in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("category-\(category.id)")
                    .accessibilityAddTraits(selected ? .isSelected : [])
                }
            }
            .padding(.horizontal, 20)
        }
    }

    private var loadingState: some View {
        VStack(spacing: 12) {
            ProgressView().tint(MyTVStyle.accent).scaleEffect(1.15)
            Text("正在加载片库…")
                .font(.subheadline)
                .foregroundStyle(MyTVStyle.secondaryText)
        }
        .frame(maxWidth: .infinity, minHeight: 280)
        .accessibilityIdentifier("home-loading-state")
    }

    @ViewBuilder
    private func sectionList(_ list: [MediaSection]) -> some View {
        LazyVStack(alignment: .leading, spacing: 26) {
            ForEach(list) { section in
                if !section.items.isEmpty {
                    VStack(alignment: .leading, spacing: 13) {
                        HStack {
                            SectionHeading(title: section.title)
                            Spacer()
                            Image(systemName: "chevron.right")
                                .font(.caption.weight(.bold))
                                .foregroundStyle(MyTVStyle.mutedText)
                                .accessibilityHidden(true)
                        }
                        .padding(.horizontal, 20)

                        ScrollView(.horizontal, showsIndicators: false) {
                            LazyHStack(alignment: .top, spacing: 14) {
                                ForEach(section.items) { item in
                                    MediaPosterCard(item: item)
                                }
                            }
                            .padding(.horizontal, 20)
                            .padding(.bottom, 2)
                        }
                    }
                }
            }
        }
    }

    @MainActor
    private func loadHome() async {
        guard let client = settings.client else {
            sections = []
            isLoading = false
            errorMessage = "请先在设置中填写有效的 HTTPS 服务地址。"
            return
        }

        isLoading = true
        errorMessage = nil
        do {
            let loaded = try await client.home(category: selectedCategory)
            try Task.checkCancellation()
            sections = loaded
            isLoading = false
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled else { return }
            errorMessage = userFacingNetworkMessage(error)
            isLoading = false
        }
    }
}
