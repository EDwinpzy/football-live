import AVKit
import Combine
import SwiftUI

struct PlaybackView: View {
    @EnvironmentObject private var settings: AppSettings
    @EnvironmentObject private var license: LicenseStore
    @EnvironmentObject private var library: LibraryStore
    @Environment(\.dismiss) private var dismiss

    let route: PlaybackRoute

    @State private var player: AVPlayer?
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var isLocked = false
    @State private var requestGeneration = 0
    @State private var availableSources: [MediaSource]
    @State private var selectedSource: MediaSource?
    @State private var selectedEpisode: Episode?
    @State private var isSourcePickerPresented = false
    @State private var savedPlaybackTime: TimeInterval = 0
    @State private var resolutionTask: Task<Void, Never>?
    @State private var stallCheckTask: Task<Void, Never>?
    @State private var relayFallbackURL: URL?
    @State private var isDirectAttempt = false
    @State private var relayFallbackAttempted = false

    init(route: PlaybackRoute) {
        self.route = route
        _availableSources = State(initialValue: route.availableSources)
        _selectedSource = State(initialValue: route.source)
        _selectedEpisode = State(initialValue: route.episode)
    }

    var body: some View {
        VStack(spacing: 0) {
            if let player {
                VideoPlayer(player: player)
                    .frame(maxWidth: .infinity)
                    .aspectRatio(16 / 9, contentMode: .fit)
                    .background(.black)
                    .accessibilityIdentifier("native-video-player")
            } else {
                ZStack {
                    Color.black
                    if isLoading && !isLocked {
                        VStack(spacing: 12) {
                            ProgressView().tint(MyTVStyle.accent)
                            Text("正在连接播放线路…")
                                .font(.subheadline)
                                .foregroundStyle(MyTVStyle.secondaryText)
                            if hasSourcePicker {
                                Button("选择线路或集数") { isSourcePickerPresented = true }
                                    .buttonStyle(AccentButtonStyle())
                                    .accessibilityIdentifier("player-source-picker-loading")
                            }
                        }
                    } else if isLocked {
                        ScreenStateView(
                            symbol: "lock.fill",
                            title: "播放需要激活",
                            message: "请先在设置页完成自用版激活，再开始播放。",
                            actionTitle: "返回",
                            action: { dismiss() }
                        )
                        .accessibilityIdentifier("player-license-gate")
                    } else if let errorMessage {
                        ScreenStateView(
                            symbol: "play.slash.fill",
                            title: "播放失败",
                            message: errorMessage,
                            actionTitle: hasSourcePicker ? "选择线路重试" : "返回",
                            action: {
                                if hasSourcePicker {
                                    isSourcePickerPresented = true
                                } else {
                                    dismiss()
                                }
                            }
                        )
                        .accessibilityIdentifier("player-error-state")
                    }
                }
                .frame(maxWidth: .infinity)
                .aspectRatio(16 / 9, contentMode: .fit)
            }

            VStack(alignment: .leading, spacing: 8) {
                Text(playerTitle)
                    .font(.headline)
                    .foregroundStyle(.white)
                    .accessibilityIdentifier("player-title")
                Text(playerDescription)
                    .font(.caption)
                    .foregroundStyle(MyTVStyle.secondaryText)
            }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            Spacer(minLength: 0)
        }
        .background(MyTVStyle.background)
        .navigationTitle("正在播放")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: route.id) {
            guard !Task.isCancelled else { return }
            await MainActor.run {
                guard !Task.isCancelled else { return }
                startPlayback(source: route.source, episode: route.episode)
            }
        }
        .onDisappear {
            requestGeneration += 1
            resolutionTask?.cancel()
            resolutionTask = nil
            stallCheckTask?.cancel()
            stallCheckTask = nil
            isSourcePickerPresented = false
            relayFallbackURL = nil
            isDirectAttempt = false
            tearDownPlayer()
        }
        .onReceive(playerStatusPublisher) { status in
            guard status.status == .failed,
                  status.item === player?.currentItem else { return }
            recoverOrShowFailure(for: status.item)
        }
        .onReceive(NotificationCenter.default.publisher(for: .AVPlayerItemFailedToPlayToEndTime).receive(on: DispatchQueue.main)) { notification in
            guard let failedItem = notification.object as? AVPlayerItem,
                  failedItem === player?.currentItem else { return }
            recoverOrShowFailure(for: failedItem)
        }
        .onReceive(NotificationCenter.default.publisher(for: .AVPlayerItemNewErrorLogEntry).receive(on: DispatchQueue.main)) { notification in
            guard isDirectAttempt,
                  let failedItem = notification.object as? AVPlayerItem,
                  failedItem === player?.currentItem else { return }
            recoverOrShowFailure(for: failedItem)
        }
        .onReceive(NotificationCenter.default.publisher(for: .AVPlayerItemPlaybackStalled).receive(on: DispatchQueue.main)) { notification in
            guard route.detailRef != nil,
                  let stalledItem = notification.object as? AVPlayerItem,
                  stalledItem === player?.currentItem else { return }
            scheduleStallCheck(for: stalledItem, generation: requestGeneration, after: 4_000_000_000)
        }
        .onChange(of: license.isActive) { active in
            guard !active else { return }
            requestGeneration += 1
            resolutionTask?.cancel()
            resolutionTask = nil
            stallCheckTask?.cancel()
            stallCheckTask = nil
            isSourcePickerPresented = false
            relayFallbackURL = nil
            isDirectAttempt = false
            tearDownPlayer()
            isLoading = false
            errorMessage = nil
            isLocked = true
        }
        .toolbar {
            if hasSourcePicker && !isLocked {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("线路/集数") { isSourcePickerPresented = true }
                        .accessibilityIdentifier("player-source-picker")
                }
            }
        }
        .sheet(isPresented: $isSourcePickerPresented) {
            sourcePicker
        }
    }

    @MainActor
    private func startPlayback(source: MediaSource?, episode: Episode?, refresh: Bool = false) {
        resolutionTask?.cancel()
        resolutionTask = nil
        stallCheckTask?.cancel()
        stallCheckTask = nil
        requestGeneration += 1
        let generation = requestGeneration
        let previousSources = availableSources
        let keepsEpisode = isSameEpisode(selectedEpisode, episode, in: source)
        let resumeTime = keepsEpisode ? currentPlaybackTime : 0
        selectedSource = source
        selectedEpisode = episode
        tearDownPlayer()
        if !keepsEpisode { savedPlaybackTime = 0 }
        errorMessage = nil
        isLocked = false
        relayFallbackURL = nil
        isDirectAttempt = false
        relayFallbackAttempted = false

        guard license.isActive else {
            isLoading = false
            isLocked = true
            return
        }
        guard let client = settings.client else {
            isLoading = false
            errorMessage = "服务地址无效，请返回设置页检查地址。"
            return
        }

        isLoading = true
        resolutionTask = Task { @MainActor in
            do {
                let upstreamURL: URL
                if let channelURL = route.channelURL {
                    upstreamURL = URL(string: channelURL).map { client.relayURL(for: $0.absoluteString) }
                        ?? client.relayURL(for: channelURL)
                } else if let detailRef = route.detailRef,
                          let requestedSource = source,
                          let requestedEpisode = episode {
                    let resolvedSource: MediaSource
                    let resolvedEpisode: Episode
                    let hhkanRoute = BackendClient.contentRoute(detailRef: detailRef).isHHKan
                    if refresh && !hhkanRoute, let item = route.item {
                        let freshDetail = try await client.detail(item: item, refresh: true)
                        try Task.checkCancellation()
                        guard generation == requestGeneration else { return }
                        let refreshedSources = freshDetail.sources
                        guard !refreshedSources.isEmpty else {
                            availableSources = previousSources
                            throw PlaybackError.noAvailableSources
                        }
                        availableSources = refreshedSources
                        guard let refreshedSource = PlaybackSourceSelection.refreshedSource(
                            matching: requestedSource,
                            in: refreshedSources
                        ), let refreshedEpisode = PlaybackSourceSelection.refreshedEpisode(
                            in: refreshedSource,
                            matching: requestedEpisode
                        ) else {
                            throw PlaybackError.selectedLineUnavailable
                        }
                        resolvedSource = refreshedSource
                        resolvedEpisode = refreshedEpisode
                        selectedSource = refreshedSource
                        selectedEpisode = refreshedEpisode
                    } else {
                        resolvedSource = requestedSource
                        resolvedEpisode = requestedEpisode
                    }
                    let stream = try await client.play(
                        detailRef: detailRef,
                        source: resolvedSource,
                        episode: resolvedEpisode,
                        fresh: refresh
                    )
                    try Task.checkCancellation()
                    let directURL = stream.directURL.flatMap { url in
                        url.scheme?.lowercased() == "https" ? url : nil
                    }
                    if let directURL {
                        upstreamURL = directURL
                        relayFallbackURL = stream.url
                        isDirectAttempt = true
                    } else {
                        upstreamURL = stream.url
                    }
                } else if let match = route.footballMatch,
                          let sourceIndex = route.footballSourceIndex {
                    let stream = try await client.matchStream(match: match, sourceIndex: sourceIndex)
                    try Task.checkCancellation()
                    upstreamURL = stream.url
                } else {
                    throw PlaybackError.missingRoute
                }
                try Task.checkCancellation()
                guard generation == requestGeneration else { return }
                guard license.isActive else {
                    isLoading = false
                    isLocked = true
                    return
                }
                installPlayer(url: upstreamURL, generation: generation, resumeTime: resumeTime)
                if let item = route.item {
                    library.recordWatching(item: item)
                }
            } catch is CancellationError {
                return
            } catch {
                guard !Task.isCancelled, generation == requestGeneration else { return }
                errorMessage = (error as? PlaybackError)?.errorDescription ?? userFacingNetworkMessage(error)
                isLoading = false
            }
        }
    }

    @MainActor
    private func installPlayer(url: URL, generation: Int, resumeTime: TimeInterval = 0) {
        guard generation == requestGeneration else { return }
        guard license.isActive else {
            isLoading = false
            isLocked = true
            return
        }
        let item = AVPlayerItem(url: url)
        let newPlayer = AVPlayer(playerItem: item)
        newPlayer.automaticallyWaitsToMinimizeStalling = true
        player = newPlayer
        isLoading = false
        if resumeTime.isFinite, resumeTime > 0 {
            newPlayer.seek(to: CMTime(seconds: resumeTime, preferredTimescale: 600)) { _ in
                Task { @MainActor in
                    guard generation == requestGeneration, player === newPlayer else { return }
                    newPlayer.play()
                }
            }
        } else {
            newPlayer.play()
        }
        if route.detailRef != nil {
            scheduleStallCheck(for: item, generation: generation, after: 12_000_000_000)
        }
    }

    @MainActor
    private func recoverOrShowFailure(for failedItem: AVPlayerItem) {
        guard failedItem === player?.currentItem else { return }
        stallCheckTask?.cancel()
        stallCheckTask = nil
        guard license.isActive else {
            tearDownPlayer()
            isLoading = false
            isLocked = true
            return
        }
        if isDirectAttempt,
           !relayFallbackAttempted,
           let relayFallbackURL {
            relayFallbackAttempted = true
            isDirectAttempt = false
            self.relayFallbackURL = nil
            tearDownPlayer()
            isLoading = true
            errorMessage = nil
            installPlayer(url: relayFallbackURL, generation: requestGeneration)
            return
        }
        presentPlaybackFailure()
    }

    private var hasSourcePicker: Bool {
        route.detailRef != nil && !availableSources.isEmpty
    }

    private var playerTitle: String {
        guard let item = route.item, let selectedEpisode else { return route.title }
        return "\(item.title) · \(selectedEpisode.name)"
    }

    private var currentPlaybackTime: TimeInterval {
        guard let player else { return savedPlaybackTime }
        let seconds = player.currentTime().seconds
        return seconds.isFinite ? max(0, seconds) : savedPlaybackTime
    }

    private func isSameEpisode(_ lhs: Episode?, _ rhs: Episode?, in nextSource: MediaSource?) -> Bool {
        guard let lhs, let rhs else { return false }
        if lhs.id == rhs.id { return true }
        let leftName = lhs.name.trimmingCharacters(in: .whitespacesAndNewlines)
        let rightName = rhs.name.trimmingCharacters(in: .whitespacesAndNewlines)
        if !leftName.isEmpty, leftName.caseInsensitiveCompare(rightName) == .orderedSame {
            return true
        }
        guard let selectedSource, let nextSource else { return false }
        guard let currentIndex = selectedSource.episodes.firstIndex(of: lhs),
              let nextIndex = nextSource.episodes.firstIndex(of: rhs) else { return false }
        return currentIndex == nextIndex
    }

    private var sourcePicker: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    ForEach(availableSources) { source in
                        VStack(alignment: .leading, spacing: 11) {
                            Button {
                                guard let episode = PlaybackSourceSelection.episode(
                                    in: source,
                                    matching: selectedEpisode,
                                    from: selectedSource
                                ) else { return }
                                select(source: source, episode: episode)
                            } label: {
                                HStack {
                                    Label(
                                        source.name,
                                        systemImage: source.id == selectedSource?.id
                                            ? "checkmark.circle.fill"
                                            : "dot.radiowaves.left.and.right"
                                    )
                                    .font(.subheadline.weight(.semibold))
                                    .foregroundStyle(.white)
                                    Spacer()
                                    Text("\(source.episodes.count) 集")
                                        .font(.caption)
                                        .foregroundStyle(MyTVStyle.secondaryText)
                                }
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .disabled(source.episodes.isEmpty)
                            .accessibilityIdentifier("player-source-\(source.id)")

                            if source.episodes.isEmpty {
                                Text("这条线路暂时没有集数。")
                                    .font(.footnote)
                                    .foregroundStyle(MyTVStyle.secondaryText)
                            } else {
                                LazyVGrid(columns: [GridItem(.adaptive(minimum: 86), spacing: 9)], alignment: .leading, spacing: 9) {
                                    ForEach(source.episodes) { episode in
                                        Button {
                                            select(source: source, episode: episode)
                                        } label: {
                                            Text(episode.name)
                                                .font(.caption.weight(.semibold))
                                                .foregroundStyle(.white)
                                                .lineLimit(1)
                                                .frame(maxWidth: .infinity)
                                                .padding(.horizontal, 10)
                                                .padding(.vertical, 11)
                                                .background(
                                                    source.id == selectedSource?.id && episode.id == selectedEpisode?.id
                                                        ? MyTVStyle.accent.opacity(0.76)
                                                        : MyTVStyle.background,
                                                    in: RoundedRectangle(cornerRadius: 10)
                                                )
                                        }
                                        .buttonStyle(.plain)
                                        .accessibilityIdentifier("player-episode-\(source.id)-\(episode.id)")
                                    }
                                }
                            }
                        }
                        .padding(15)
                        .background(MyTVStyle.surface, in: RoundedRectangle(cornerRadius: 15, style: .continuous))
                    }
                }
                .padding(18)
            }
            .background(MyTVStyle.background)
            .navigationTitle("选择线路和集数")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("完成") { isSourcePickerPresented = false }
                        .foregroundStyle(MyTVStyle.accent)
                }
            }
        }
        .presentationDetents([.large])
        .presentationDragIndicator(.visible)
        .preferredColorScheme(.dark)
    }

    @MainActor
    private func select(source: MediaSource, episode: Episode) {
        isSourcePickerPresented = false
        startPlayback(source: source, episode: episode, refresh: true)
    }

    @MainActor
    private func scheduleStallCheck(for item: AVPlayerItem, generation: Int, after delay: UInt64) {
        stallCheckTask?.cancel()
        stallCheckTask = Task { @MainActor in
            do {
                try await Task.sleep(nanoseconds: delay)
            } catch {
                return
            }
            guard !Task.isCancelled,
                  generation == requestGeneration,
                  item === player?.currentItem,
                  player?.timeControlStatus == .waitingToPlayAtSpecifiedRate else { return }
            recoverOrShowFailure(for: item)
        }
    }

    @MainActor
    private func tearDownPlayer() {
        if let player {
            let seconds = player.currentTime().seconds
            if seconds.isFinite {
                savedPlaybackTime = max(0, seconds)
            }
        }
        player?.pause()
        player?.replaceCurrentItem(with: nil)
        player = nil
    }

    private var playerDescription: String {
        if route.footballMatch != nil { return "足球直播" }
        if route.channelURL != nil { return "IPTV 直播" }
        return selectedSource?.name ?? "影视播放"
    }

    private var playerStatusPublisher: AnyPublisher<(item: AVPlayerItem, status: AVPlayerItem.Status), Never> {
        guard let item = player?.currentItem else {
            return Empty<(item: AVPlayerItem, status: AVPlayerItem.Status), Never>().eraseToAnyPublisher()
        }
        return item.publisher(for: \.status, options: [.initial, .new])
            .map { (item: item, status: $0) }
            .receive(on: DispatchQueue.main)
            .eraseToAnyPublisher()
    }

    @MainActor
    private func presentPlaybackFailure() {
        errorMessage = "当前线路无法播放或已中断。返回后可以选择其他线路。"
        isLoading = false
        relayFallbackURL = nil
        isDirectAttempt = false
        stallCheckTask?.cancel()
        stallCheckTask = nil
        tearDownPlayer()
    }
}

private enum PlaybackError: LocalizedError {
    case missingRoute
    case noAvailableSources
    case selectedLineUnavailable

    var errorDescription: String? {
        switch self {
        case .missingRoute:
            return "播放请求不完整，请返回详情页重新选择线路和集数。"
        case .noAvailableSources:
            return "刷新后的详情没有可用线路，仍可打开当前线路面板重试或返回详情页。"
        case .selectedLineUnavailable:
            return "刷新后的线路列表中找不到当前选择，请打开线路面板选择一条可用线路重试。"
        }
    }
}
