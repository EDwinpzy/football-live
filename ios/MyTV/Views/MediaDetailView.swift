import SwiftUI

struct MediaDetailView: View {
    @EnvironmentObject private var settings: AppSettings
    @EnvironmentObject private var library: LibraryStore

    let item: MediaItem
    let fixtureDetail: MediaDetail?

    @State private var loadedDetail: MediaDetail?
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var retryToken = 0

    init(item: MediaItem, fixtureDetail: MediaDetail? = nil) {
        self.item = item
        self.fixtureDetail = fixtureDetail
    }

    private var detail: MediaDetail? { fixtureDetail ?? loadedDetail }
    private var displayedItem: MediaItem { detail?.item ?? item }

    var body: some View {
        Group {
            if let detail {
                detailContent(detail)
            } else if isLoading {
                ScreenStateView(symbol: "film", title: "正在加载详情…", message: item.title)
                    .overlay { ProgressView().tint(MyTVStyle.accent).offset(y: 58) }
                    .accessibilityIdentifier("detail-loading-state")
            } else if let errorMessage {
                ScreenStateView(
                    symbol: "wifi.exclamationmark",
                    title: "详情加载失败",
                    message: errorMessage,
                    actionTitle: "重试",
                    action: { retryToken += 1 }
                )
                .accessibilityIdentifier("detail-error-state")
            } else {
                ScreenStateView(symbol: "film", title: "暂无详情", message: "服务没有返回影片详情。")
                    .accessibilityIdentifier("detail-empty-state")
            }
        }
        .background(MyTVStyle.background)
        .navigationTitle(displayedItem.title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if detail != nil {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button {
                        library.toggleFavorite(displayedItem)
                    } label: {
                        Image(systemName: library.isFavorite(displayedItem) ? "bookmark.fill" : "bookmark")
                            .foregroundStyle(library.isFavorite(displayedItem) ? MyTVStyle.accent : .white)
                    }
                    .accessibilityLabel(library.isFavorite(displayedItem) ? "取消收藏" : "加入收藏")
                    .accessibilityIdentifier("detail-favorite-button")
                }
            }
        }
        .task(id: "\(item.id)-\(retryToken)") {
            guard fixtureDetail == nil else { return }
            await loadDetail()
        }
        .accessibilityIdentifier("detail-screen")
    }

    private func detailContent(_ detail: MediaDetail) -> some View {
        let orderedSources = PlaybackSourceSelection.ordered(
            detail.sources,
            preferring: settings.rememberedVODSource(for: detail.item.id)
        )
        return ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack(alignment: .top, spacing: 18) {
                    PosterArtwork(item: detail.item, width: 138, height: 198)
                        .accessibilityIdentifier("detail-poster")
                    VStack(alignment: .leading, spacing: 11) {
                        Text(detail.item.title)
                            .font(.title2.weight(.bold))
                            .foregroundStyle(.white)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("detail-title")
                        HStack(spacing: 10) {
                            if !detail.item.year.isEmpty { metadataPill(detail.item.year) }
                            if !detail.item.rating.isEmpty {
                                Label(detail.item.rating, systemImage: "star.fill")
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(.yellow)
                            }
                        }
                        Text("选择线路和集数开始播放")
                            .font(.footnote)
                            .foregroundStyle(MyTVStyle.secondaryText)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                        if !detail.sources.isEmpty {
                            Label("\(detail.sources.count) 条线路", systemImage: "rectangle.stack.fill")
                                .font(.caption.weight(.medium))
                                .foregroundStyle(MyTVStyle.accent)
                        }
                    }
                    .frame(maxWidth: .infinity, minHeight: 188, alignment: .leading)
                }

                if !detail.summary.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    VStack(alignment: .leading, spacing: 10) {
                        SectionHeading(title: "简介")
                        Text(detail.summary)
                            .font(.subheadline)
                            .foregroundStyle(MyTVStyle.secondaryText)
                            .lineSpacing(4)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("detail-summary")
                    }
                }

                VStack(alignment: .leading, spacing: 16) {
                    SectionHeading(title: "选择线路")
                    if orderedSources.isEmpty {
                        Text("当前影片没有可播放线路。")
                            .font(.subheadline)
                            .foregroundStyle(MyTVStyle.secondaryText)
                            .padding(16)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(MyTVStyle.surface, in: RoundedRectangle(cornerRadius: 14))
                            .accessibilityIdentifier("detail-no-sources")
                    } else {
                        ForEach(orderedSources) { source in
                            sourceCard(source, item: detail.item, availableSources: orderedSources)
                        }
                    }
                }
            }
            .padding(20)
            .frame(maxWidth: 760, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .accessibilityIdentifier("detail-loaded-content")
    }

    private func sourceCard(_ source: MediaSource, item: MediaItem, availableSources: [MediaSource]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label(source.name, systemImage: "dot.radiowaves.left.and.right")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.white)
                Spacer()
                Text("\(source.episodes.count) 集")
                    .font(.caption)
                    .foregroundStyle(MyTVStyle.secondaryText)
            }

            if source.episodes.isEmpty {
                Text("这条线路暂时没有集数。")
                    .font(.footnote)
                    .foregroundStyle(MyTVStyle.secondaryText)
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 76), spacing: 9)], alignment: .leading, spacing: 9) {
                    ForEach(source.episodes) { episode in
                        NavigationLink(value: PlaybackRoute(
                            item: item,
                            detailRef: item.detailRef,
                            source: source,
                            episode: episode,
                            availableSources: availableSources
                        )) {
                            Text(episode.name)
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.white)
                                .lineLimit(1)
                                .frame(maxWidth: .infinity)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 11)
                                .background(MyTVStyle.background, in: RoundedRectangle(cornerRadius: 10))
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("episode-\(source.id)-\(episode.id)")
                    }
                }
            }
        }
        .padding(15)
        .background(MyTVStyle.surface, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .accessibilityIdentifier("source-card-\(source.id)")
    }

    private func metadataPill(_ value: String) -> some View {
        Text(value)
            .font(.caption.weight(.semibold))
            .foregroundStyle(MyTVStyle.secondaryText)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(MyTVStyle.surface, in: Capsule())
    }

    @MainActor
    private func loadDetail() async {
        guard let client = settings.client else {
            isLoading = false
            errorMessage = "请先在设置中填写有效的 HTTPS 服务地址。"
            return
        }
        isLoading = loadedDetail == nil
        errorMessage = nil
        do {
            let loaded = try await client.detail(item: item, refresh: true)
            try Task.checkCancellation()
            loadedDetail = loaded
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

struct PlaybackRoute: Hashable, Identifiable {
    let id: String
    let title: String
    let item: MediaItem?
    let detailRef: String?
    let source: MediaSource?
    let episode: Episode?
    let availableSources: [MediaSource]
    let channelURL: String?
    let footballMatch: FootballMatch?
    let footballStream: FootballStreamReference?

    init(
        item: MediaItem,
        detailRef: String,
        source: MediaSource,
        episode: Episode,
        availableSources: [MediaSource] = []
    ) {
        self.id = "vod-\(item.id)-\(source.id)-\(episode.id)"
        self.title = "\(item.title) · \(episode.name)"
        self.item = item
        self.detailRef = detailRef
        self.source = source
        self.episode = episode
        self.availableSources = availableSources.isEmpty ? [source] : availableSources
        self.channelURL = nil
        self.footballMatch = nil
        self.footballStream = nil
    }

    init(channel: TVChannel, streamURL: String) {
        self.id = "live-\(channel.id)-\(streamURL)"
        self.title = channel.name
        self.item = nil
        self.detailRef = nil
        self.source = nil
        self.episode = nil
        self.availableSources = []
        self.channelURL = streamURL
        self.footballMatch = nil
        self.footballStream = nil
    }

    init(footballMatch: FootballMatch, stream: FootballStreamReference) {
        self.id = "football-\(footballMatch.id)-\(stream.id)"
        self.title = "\(footballMatch.home) vs \(footballMatch.away)"
        self.item = nil
        self.detailRef = nil
        self.source = nil
        self.episode = nil
        self.availableSources = []
        self.channelURL = nil
        self.footballMatch = footballMatch
        self.footballStream = stream
    }
}
