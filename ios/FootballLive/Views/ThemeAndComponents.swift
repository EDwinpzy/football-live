import SwiftUI

enum FootballLiveStyle {
    static let background = Color(red: 0.035, green: 0.043, blue: 0.067)
    static let surface = Color(red: 0.075, green: 0.09, blue: 0.125)
    static let accent = Color(red: 0.98, green: 0.27, blue: 0.31)
    static let secondaryText = Color(red: 0.66, green: 0.70, blue: 0.77)
    static let mutedText = Color(red: 0.43, green: 0.48, blue: 0.56)
}

extension View {
    func footballLiveScreen() -> some View {
        self
            .background(FootballLiveStyle.background.ignoresSafeArea())
            .preferredColorScheme(.dark)
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
                .foregroundStyle(FootballLiveStyle.accent)
                .accessibilityHidden(true)
            Text(title)
                .font(.title3.weight(.semibold))
                .foregroundStyle(.white)
            Text(message)
                .font(.subheadline)
                .foregroundStyle(FootballLiveStyle.secondaryText)
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
            .background(FootballLiveStyle.accent.opacity(configuration.isPressed ? 0.75 : 1), in: Capsule())
    }
}

func userFacingNetworkMessage(_ error: Error) -> String {
    if let backendError = error as? BackendError {
        switch backendError {
        case .certificateTrust:
            return "无法验证服务证书。请先安装并完全信任项目提供的公共迁移 CA，然后重试。"
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
            return "无法验证服务证书。请先安装并信任项目提供的公共迁移 CA，然后重试。"
        case .notConnectedToInternet, .networkConnectionLost, .timedOut,
             .cannotConnectToHost, .cannotFindHost:
            return "暂时无法连接服务，请检查网络和服务地址后重试。"
        default:
            break
        }
    }
    return "服务暂时不可用，请稍后重试并检查服务连接。"
}
