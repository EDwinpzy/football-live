import SwiftUI

struct FootballView: View {
    @EnvironmentObject private var settings: AppSettings

    @State private var matches: [FootballMatch] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var retryToken = 0
    @State private var selectedMatch: FootballMatch?

    var body: some View {
        Group {
            if isLoading {
                ScreenStateView(symbol: "soccerball", title: "正在加载比赛…", message: "")
                    .overlay { ProgressView().tint(MyTVStyle.accent).offset(y: 58) }
                    .accessibilityIdentifier("football-loading-state")
            } else if let errorMessage {
                ScreenStateView(
                    symbol: "wifi.exclamationmark",
                    title: "比赛加载失败",
                    message: errorMessage,
                    actionTitle: "重试",
                    action: { retryToken += 1 }
                )
                .accessibilityIdentifier("football-error-state")
            } else if matches.isEmpty {
                ScreenStateView(symbol: "soccerball", title: "暂无比赛", message: "服务暂时没有提供比赛列表。")
                    .accessibilityIdentifier("football-empty-state")
            } else {
                ScrollView {
                    LazyVStack(spacing: 12) {
                        ForEach(matches) { match in
                            matchCard(match)
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 14)
                }
                .accessibilityIdentifier("football-match-list")
            }
        }
        .background(MyTVStyle.background)
        .navigationTitle("足球")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: retryToken) { await loadMatches() }
        .sheet(item: $selectedMatch) { match in
            sourcePicker(for: match)
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
    }

    private func matchCard(_ match: FootballMatch) -> some View {
        VStack(alignment: .leading, spacing: 15) {
            HStack(alignment: .center) {
                Text(match.league.isEmpty ? "比赛" : match.league)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(MyTVStyle.secondaryText)
                    .lineLimit(1)
                Spacer()
                Text(match.status.isEmpty ? match.time : match.status + (match.minute.isEmpty ? "" : " · \(match.minute)"))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(match.status.localizedCaseInsensitiveContains("live") || match.status.contains("进行")
                                     ? MyTVStyle.accent : MyTVStyle.secondaryText)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 5)
                    .background(MyTVStyle.background, in: Capsule())
            }

            HStack(spacing: 8) {
                Text(match.home)
                    .font(.headline.weight(.semibold))
                    .foregroundStyle(.white)
                    .multilineTextAlignment(.trailing)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                Text(match.score.isEmpty ? "VS" : match.score)
                    .font(.caption.weight(.heavy))
                    .foregroundStyle(MyTVStyle.mutedText)
                Text(match.away)
                    .font(.headline.weight(.semibold))
                    .foregroundStyle(.white)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            HStack {
                Label(match.time, systemImage: "clock")
                    .font(.caption)
                    .foregroundStyle(MyTVStyle.secondaryText)
                    .lineLimit(1)
                Spacer()
                Button {
                    selectedMatch = match
                } label: {
                    Label("选择线路", systemImage: "play.fill")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 13)
                        .padding(.vertical, 9)
                        .background(MyTVStyle.accent, in: Capsule())
                }
                .buttonStyle(.plain)
                .disabled(match.streams.isEmpty)
                .opacity(match.streams.isEmpty ? 0.5 : 1)
                .accessibilityIdentifier("football-select-source-\(match.id)")
            }
        }
        .padding(15)
        .background(MyTVStyle.surface, in: RoundedRectangle(cornerRadius: 17, style: .continuous))
        .accessibilityIdentifier("football-match-\(match.id)")
    }

    private func sourcePicker(for match: FootballMatch) -> some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("\(match.home) vs \(match.away)")
                            .font(.title3.weight(.bold))
                            .foregroundStyle(.white)
                        Text("\(match.league) · \(match.time)")
                            .font(.subheadline)
                            .foregroundStyle(MyTVStyle.secondaryText)
                    }
                    .padding(.bottom, 4)

                    if match.streams.isEmpty {
                        Text("当前比赛没有可用线路。")
                            .font(.subheadline)
                            .foregroundStyle(MyTVStyle.secondaryText)
                    } else {
                        ForEach(match.streams.indices, id: \.self) { index in
                            let stream = match.streams[index]
                            NavigationLink(value: PlaybackRoute(footballMatch: match, sourceIndex: index)) {
                                HStack(spacing: 12) {
                                    Image(systemName: "dot.radiowaves.left.and.right")
                                        .foregroundStyle(MyTVStyle.accent)
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(stream.name)
                                            .font(.subheadline.weight(.semibold))
                                            .foregroundStyle(.white)
                                        Text("足球直播线路")
                                            .font(.caption)
                                            .foregroundStyle(MyTVStyle.secondaryText)
                                    }
                                    Spacer()
                                    Image(systemName: "chevron.right")
                                        .font(.caption.weight(.bold))
                                        .foregroundStyle(MyTVStyle.mutedText)
                                }
                                .padding(15)
                                .background(MyTVStyle.surface, in: RoundedRectangle(cornerRadius: 14))
                            }
                            .buttonStyle(.plain)
                            .accessibilityIdentifier("football-source-\(match.id)-\(index)")
                        }
                    }
                }
                .padding(18)
            }
            .background(MyTVStyle.background)
            .navigationTitle("选择播放线路")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("完成") { selectedMatch = nil }
                        .foregroundStyle(MyTVStyle.accent)
                }
            }
            .navigationDestination(for: PlaybackRoute.self) { route in
                PlaybackView(route: route)
            }
        }
        .myTVScreen()
        .accessibilityIdentifier("football-source-picker")
    }

    @MainActor
    private func loadMatches() async {
        guard let client = settings.client else {
            matches = []
            isLoading = false
            errorMessage = "请先在设置中填写有效的 HTTPS 服务地址。"
            return
        }
        isLoading = true
        errorMessage = nil
        do {
            let loaded = try await client.matches()
            try Task.checkCancellation()
            matches = loaded
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
