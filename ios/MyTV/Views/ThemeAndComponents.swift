import SwiftUI

enum MyTVStyle {
    static let background = Color(red: 0.035, green: 0.043, blue: 0.067)
    static let surface = Color(red: 0.075, green: 0.09, blue: 0.125)
    static let elevated = Color(red: 0.11, green: 0.13, blue: 0.18)
    static let accent = Color(red: 0.98, green: 0.27, blue: 0.31)
    static let secondaryText = Color(red: 0.66, green: 0.70, blue: 0.77)
    static let mutedText = Color(red: 0.43, green: 0.48, blue: 0.56)
}

extension View {
    func myTVScreen() -> some View {
        self
            .background(MyTVStyle.background.ignoresSafeArea())
            .preferredColorScheme(.dark)
    }

    func installMyTVDestinations(
        fixtureDetail: ((MediaItem) -> MediaDetail?)? = nil
    ) -> some View {
        self
            .navigationDestination(for: MediaItem.self) { item in
                MediaDetailView(item: item, fixtureDetail: fixtureDetail?(item))
            }
            .navigationDestination(for: PlaybackRoute.self) { route in
                PlaybackView(route: route)
            }
    }
}

struct ScreenStateView: View {
    let symbol: String
    let title: String
    let message: String
    var actionTitle: String? = nil
    var action: (() -> Void)? = nil

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: symbol)
                .font(.system(size: 34, weight: .regular))
                .foregroundStyle(MyTVStyle.accent)
                .accessibilityHidden(true)
            Text(title)
                .font(.title3.weight(.semibold))
                .foregroundStyle(.white)
            Text(message)
                .font(.subheadline)
                .foregroundStyle(MyTVStyle.secondaryText)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            if let actionTitle, let action {
                Button(actionTitle, action: action)
                    .buttonStyle(AccentButtonStyle())
                    .padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(28)
    }
}

struct AccentButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 18)
            .padding(.vertical, 12)
            .background(MyTVStyle.accent.opacity(configuration.isPressed ? 0.75 : 1), in: Capsule())
    }
}

struct PosterArtwork: View {
    @EnvironmentObject private var settings: AppSettings

    let item: MediaItem
    var width: CGFloat = 132
    var height: CGFloat = 190
    var onImageLoaded: (() -> Void)? = nil

    var body: some View {
        Group {
            if let url = settings.client?.imageURL(for: item.poster) {
                AsyncImage(url: url) { phase in
                    switch phase {
                    case .success(let image):
                        image.resizable().scaledToFill()
                            .onAppear { onImageLoaded?() }
                            .accessibilityIdentifier("poster-loaded-\(item.id)")
                    case .empty:
                        fallbackArtwork
                            .accessibilityIdentifier("poster-placeholder-\(item.id)")
                    case .failure:
                        fallbackArtwork
                            .accessibilityIdentifier("poster-error-\(item.id)")
                    @unknown default:
                        fallbackArtwork
                            .accessibilityIdentifier("poster-placeholder-\(item.id)")
                    }
                }
            } else {
                fallbackArtwork
                    .accessibilityIdentifier("poster-placeholder-\(item.id)")
            }
        }
        .frame(width: width, height: height)
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(alignment: .bottomLeading) {
            LinearGradient(
                colors: [.clear, .black.opacity(0.74)],
                startPoint: .center,
                endPoint: .bottom
            )
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("海报：\(item.title)")
        .accessibilityIdentifier("poster-container-\(item.id)")
    }

    private var fallbackArtwork: some View {
        ZStack {
            LinearGradient(
                colors: [MyTVStyle.elevated, MyTVStyle.surface, Color.black.opacity(0.8)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            Image(systemName: "play.rectangle.fill")
                .font(.system(size: 28, weight: .light))
                .foregroundStyle(.white.opacity(0.35))
        }
    }
}

struct MediaPosterCard: View {
    let item: MediaItem
    @State private var posterLoaded = false

    var body: some View {
        NavigationLink(value: item) {
            VStack(alignment: .leading, spacing: 8) {
                PosterArtwork(item: item, onImageLoaded: { posterLoaded = true })
                    .overlay(alignment: .bottomLeading) {
                        if !item.rating.isEmpty {
                            Label(item.rating, systemImage: "star.fill")
                                .font(.caption2.weight(.bold))
                                .foregroundStyle(.yellow)
                                .padding(.horizontal, 8)
                                .padding(.vertical, 5)
                                .background(.black.opacity(0.62), in: Capsule())
                                .padding(8)
                        }
                    }
                Text(item.title)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.white)
                    .lineLimit(1)
                if !item.year.isEmpty {
                    Text(item.year)
                        .font(.caption)
                        .foregroundStyle(MyTVStyle.secondaryText)
                }
            }
            .frame(width: 132, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("media-card-\(item.id)")
        .accessibilityValue(posterLoaded ? "海报已加载" : "海报加载中")
    }
}

struct SectionHeading: View {
    let title: String
    var subtitle: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title)
                .font(.title3.weight(.bold))
                .foregroundStyle(.white)
            if let subtitle {
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(MyTVStyle.secondaryText)
            }
        }
    }
}

func userFacingNetworkMessage(_ error: Error) -> String {
    if let backendError = error as? BackendError {
        switch backendError {
        case .certificateTrust:
            return "无法验证服务证书。请先安装并完全信任 My TV 公共迁移 CA，然后重试。"
        case .invalidOrigin:
            return "服务地址必须是有效的 HTTPS 域名或 IP。"
        case .httpStatus(let status):
            return "服务暂时无法处理请求（HTTP \(status)），请稍后重试。"
        case .invalidResponse, .malformedPayload:
            return "服务返回的数据暂时无法读取，请稍后重试。"
        case .network:
            return "暂时无法连接服务，请检查网络和服务地址后重试。"
        }
    }
    if let urlError = error as? URLError {
        switch urlError.code {
        case .serverCertificateUntrusted, .serverCertificateHasBadDate,
             .serverCertificateHasUnknownRoot, .secureConnectionFailed:
            return "无法验证服务证书。请先安装并信任 My TV 公共迁移 CA，然后重试。"
        case .notConnectedToInternet, .networkConnectionLost, .timedOut,
             .cannotConnectToHost, .cannotFindHost:
            return "暂时无法连接服务，请检查网络和服务地址后重试。"
        default:
            break
        }
    }
    return "服务暂时不可用，请稍后重试并检查服务连接。"
}
