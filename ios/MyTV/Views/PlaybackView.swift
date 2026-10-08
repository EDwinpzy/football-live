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
    @State private var relayFallbackURL: URL?
    @State private var isDirectAttempt = false
    @State private var relayFallbackAttempted = false

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
                            actionTitle: "返回详情选择其他线路",
                            action: { dismiss() }
                        )
                        .accessibilityIdentifier("player-error-state")
                    }
                }
                .frame(maxWidth: .infinity)
                .aspectRatio(16 / 9, contentMode: .fit)
            }

            VStack(alignment: .leading, spacing: 8) {
                Text(route.title)
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
        .task(id: route.id) { await startPlayback() }
        .onDisappear {
            requestGeneration += 1
            relayFallbackURL = nil
            isDirectAttempt = false
            player?.pause()
            player = nil
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
            guard isDirectAttempt,
                  let stalledItem = notification.object as? AVPlayerItem,
                  stalledItem === player?.currentItem else { return }
            let generation = requestGeneration
            Task { @MainActor in
                do {
                    try await Task.sleep(nanoseconds: 4_000_000_000)
                } catch {
                    return
                }
                guard generation == requestGeneration,
                      isDirectAttempt,
                      stalledItem === player?.currentItem,
                      player?.timeControlStatus == .waitingToPlayAtSpecifiedRate else { return }
                recoverOrShowFailure(for: stalledItem)
            }
        }
        .onChange(of: license.isActive) { active in
            guard !active else { return }
            requestGeneration += 1
            relayFallbackURL = nil
            isDirectAttempt = false
            player?.pause()
            player = nil
            isLoading = false
            errorMessage = nil
            isLocked = true
        }
        .accessibilityIdentifier("playback-screen")
    }

    @MainActor
    private func startPlayback() async {
        requestGeneration += 1
        let generation = requestGeneration
        player?.pause()
        player = nil
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
        do {
            let upstreamURL: URL
            if let channelURL = route.channelURL {
                upstreamURL = URL(string: channelURL).map { client.relayURL(for: $0.absoluteString) }
                    ?? client.relayURL(for: channelURL)
            } else if let detailRef = route.detailRef,
                      let source = route.source,
                      let episode = route.episode {
                let stream = try await client.play(detailRef: detailRef, source: source, episode: episode)
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
            installPlayer(url: upstreamURL, generation: generation)
            if let item = route.item {
                library.recordWatching(item: item)
            }
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled, generation == requestGeneration else { return }
            errorMessage = userFacingNetworkMessage(error)
            isLoading = false
        }
    }

    @MainActor
    private func installPlayer(url: URL, generation: Int) {
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
        newPlayer.play()
    }

    @MainActor
    private func recoverOrShowFailure(for failedItem: AVPlayerItem) {
        guard failedItem === player?.currentItem else { return }
        guard license.isActive else {
            player?.pause()
            player = nil
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
            player?.pause()
            player = nil
            isLoading = true
            errorMessage = nil
            installPlayer(url: relayFallbackURL, generation: requestGeneration)
            return
        }
        presentPlaybackFailure()
    }

    private var playerDescription: String {
        if route.footballMatch != nil { return "足球直播" }
        if route.channelURL != nil { return "IPTV 直播" }
        return route.source?.name ?? "影视播放"
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
        player?.pause()
        player = nil
    }
}

private enum PlaybackError: LocalizedError {
    case missingRoute

    var errorDescription: String? {
        "播放请求不完整，请返回详情页重新选择线路和集数。"
    }
}
