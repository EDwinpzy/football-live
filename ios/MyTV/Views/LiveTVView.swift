import SwiftUI

private struct ChannelGroup: Identifiable {
    let name: String
    let channels: [TVChannel]
    var id: String { name }
}

struct LiveTVView: View {
    @EnvironmentObject private var settings: AppSettings

    @State private var channels: [TVChannel] = []
    @State private var isLoading = true
    @State private var errorMessage: String?
    @State private var retryToken = 0

    private var groupedChannels: [ChannelGroup] {
        var order: [String] = []
        var groups: [String: [TVChannel]] = [:]
        for channel in channels {
            let group = channel.group.isEmpty ? "其他" : channel.group
            if groups[group] == nil { order.append(group) }
            groups[group, default: []].append(channel)
        }
        return order.map { ChannelGroup(name: $0, channels: groups[$0] ?? []) }
    }

    var body: some View {
        Group {
            if isLoading {
                ScreenStateView(symbol: "dot.radiowaves.left.and.right", title: "正在加载电视台…", message: "")
                    .overlay { ProgressView().tint(MyTVStyle.accent).offset(y: 58) }
                    .accessibilityIdentifier("live-loading-state")
            } else if let errorMessage {
                ScreenStateView(
                    symbol: "wifi.exclamationmark",
                    title: "电视台加载失败",
                    message: errorMessage,
                    actionTitle: "重试",
                    action: { retryToken += 1 }
                )
                .accessibilityIdentifier("live-error-state")
            } else if channels.isEmpty {
                ScreenStateView(symbol: "tv", title: "暂无电视台", message: "服务暂时没有提供 IPTV 频道。")
                    .accessibilityIdentifier("live-empty-state")
            } else {
                List {
                    ForEach(groupedChannels) { group in
                        Section {
                            ForEach(group.channels) { channel in
                                channelRow(channel)
                                    .listRowBackground(MyTVStyle.surface)
                            }
                        } header: {
                            Text(group.name)
                                .foregroundStyle(MyTVStyle.secondaryText)
                        }
                    }
                }
                .scrollContentBackground(.hidden)
                .listStyle(.insetGrouped)
                .accessibilityIdentifier("live-channel-list")
            }
        }
        .background(MyTVStyle.background)
        .navigationTitle("电视")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: retryToken) { await loadChannels() }
    }

    private func channelRow(_ channel: TVChannel) -> some View {
        Group {
            if let firstURL = channel.urls.first, !firstURL.isEmpty {
                NavigationLink(value: PlaybackRoute(channel: channel, streamURL: firstURL)) {
                    rowLabel(channel)
                }
            } else {
                rowLabel(channel)
                    .opacity(0.55)
            }
        }
        .accessibilityIdentifier("channel-\(channel.id)")
    }

    private func rowLabel(_ channel: TVChannel) -> some View {
        HStack(spacing: 13) {
            Image(systemName: "dot.radiowaves.left.and.right")
                .font(.system(size: 18, weight: .medium))
                .foregroundStyle(MyTVStyle.accent)
                .frame(width: 38, height: 38)
                .background(MyTVStyle.background, in: RoundedRectangle(cornerRadius: 11))
            Text(channel.name)
                .font(.body.weight(.medium))
                .foregroundStyle(.white)
            Spacer()
            if channel.urls.isEmpty {
                Text("暂不可播")
                    .font(.caption)
                    .foregroundStyle(MyTVStyle.mutedText)
            }
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
    }

    @MainActor
    private func loadChannels() async {
        guard let client = settings.client else {
            channels = []
            isLoading = false
            errorMessage = "请先在设置中填写有效的 HTTPS 服务地址。"
            return
        }
        isLoading = true
        errorMessage = nil
        do {
            let loaded = try await client.channels()
            try Task.checkCancellation()
            channels = loaded
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
