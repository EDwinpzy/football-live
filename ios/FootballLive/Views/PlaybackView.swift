import AVKit
import Combine
import SwiftUI

struct PlaybackView: View {
    @EnvironmentObject private var settings: AppSettings
    @Environment(\.dismiss) private var dismiss

    let route: PlaybackRoute

    @State private var player: AVPlayer?
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var requestGeneration = 0

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
                    if isLoading {
                        VStack(spacing: 12) {
                            ProgressView().tint(FootballLiveStyle.accent)
                            Text("正在连接直播线路…")
                                .font(.subheadline)
                                .foregroundStyle(FootballLiveStyle.secondaryText)
                        }
                    } else if let errorMessage {
                        ScreenStateView(
                            symbol: "play.slash.fill",
                            title: "直播连接失败",
                            message: errorMessage,
                            actionTitle: "返回线路列表",
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
                    .foregroundStyle(FootballLiveStyle.secondaryText)
                    .accessibilityIdentifier("player-metadata")
            }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            Spacer(minLength: 0)
        }
        .background(FootballLiveStyle.background)
        .navigationTitle("足球直播")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: route.id) { await startPlayback() }
        .onDisappear {
            requestGeneration += 1
            tearDownPlayer()
        }
        .onReceive(playerStatusPublisher) { status in
            guard status.status == .failed,
                  status.item === player?.currentItem else { return }
            showPlaybackFailure()
        }
        .onReceive(NotificationCenter.default.publisher(for: .AVPlayerItemFailedToPlayToEndTime).receive(on: DispatchQueue.main)) { notification in
            guard let failedItem = notification.object as? AVPlayerItem,
                  failedItem === player?.currentItem else { return }
            showPlaybackFailure()
        }
    }

    private var playerDescription: String {
        [route.match.league, route.match.displayStatus, route.stream.name]
            .filter { !$0.isEmpty }
            .joined(separator: " · ")
    }

    @MainActor
    private func startPlayback() async {
        requestGeneration += 1
        let generation = requestGeneration
        errorMessage = nil
        isLoading = true
        tearDownPlayer()

        guard let client = settings.client else {
            isLoading = false
            errorMessage = "服务地址无效，请返回设置页检查地址。"
            return
        }

        do {
            let stream = try await client.matchStream(match: route.match, source: route.stream)
            try Task.checkCancellation()
            guard generation == requestGeneration else { return }
            installPlayer(url: stream.url)
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled, generation == requestGeneration else { return }
            errorMessage = userFacingNetworkMessage(error)
            isLoading = false
        }
    }

    @MainActor
    private func installPlayer(url: URL) {
        let item = AVPlayerItem(url: url)
        let newPlayer = AVPlayer(playerItem: item)
        newPlayer.automaticallyWaitsToMinimizeStalling = true
        player = newPlayer
        isLoading = false
        newPlayer.play()
    }

    @MainActor
    private func showPlaybackFailure() {
        errorMessage = "当前直播线路无法播放或已中断。返回线路列表后可以选择其他线路。"
        isLoading = false
        tearDownPlayer()
    }

    @MainActor
    private func tearDownPlayer() {
        player?.pause()
        player?.replaceCurrentItem(with: nil)
        player = nil
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
}
