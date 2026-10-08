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
            player?.pause()
            player = nil
        }
        .onReceive(playerStatusPublisher) { status in
            guard status.status == .failed,
                  status.item === player?.currentItem else { return }
            presentPlaybackFailure()
        }
        .onReceive(NotificationCenter.default.publisher(for: .AVPlayerItemFailedToPlayToEndTime)) { notification in
            guard let failedItem = notification.object as? AVPlayerItem,
                  failedItem === player?.currentItem else { return }
            presentPlaybackFailure()
        }
        .onChange(of: license.isActive) { active in
            guard !active else { return }
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
        player?.pause()
        player = nil
        errorMessage = nil
        isLocked = false

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
                upstreamURL = stream.url
            } else if let match = route.footballMatch,
                      let sourceIndex = route.footballSourceIndex {
                let stream = try await client.matchStream(match: match, sourceIndex: sourceIndex)
                try Task.checkCancellation()
                upstreamURL = stream.url
            } else {
                throw PlaybackError.missingRoute
            }
            try Task.checkCancellation()
            guard license.isActive else {
                isLoading = false
                isLocked = true
                return
            }
            let item = AVPlayerItem(url: upstreamURL)
            let newPlayer = AVPlayer(playerItem: item)
            newPlayer.automaticallyWaitsToMinimizeStalling = true
            player = newPlayer
            if let item = route.item {
                library.recordWatching(item: item)
            }
            isLoading = false
            newPlayer.play()
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled else { return }
            errorMessage = userFacingNetworkMessage(error)
            isLoading = false
        }
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
            .eraseToAnyPublisher()
    }

    @MainActor
    private func presentPlaybackFailure() {
        errorMessage = "当前线路无法播放或已中断。返回后可以选择其他线路。"
        isLoading = false
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
