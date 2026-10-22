import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var settings: AppSettings

    var isFirstRun = false

    @State private var originDraft = ""
    @State private var saveMessage: String?
    @State private var saveFailed = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(isFirstRun ? "欢迎使用 足球直播" : "设置")
                        .font(.largeTitle.weight(.bold))
                        .foregroundStyle(.white)
                        .accessibilityIdentifier("settings-title")
                    Text(isFirstRun
                         ? "先确认服务地址，随后即可查看足球比赛和直播线路。首次连接需要安装并信任项目提供的公共迁移 CA。"
                         : "管理足球直播服务连接。")
                        .font(.subheadline)
                        .foregroundStyle(FootballLiveStyle.secondaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }

                serviceCard
                connectionHelp
            }
            .padding(.horizontal, 20)
            .padding(.top, 24)
            .padding(.bottom, 40)
            .frame(maxWidth: 620, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(FootballLiveStyle.background)
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { originDraft = settings.originText }
        .onChange(of: settings.originText) { newValue in originDraft = newValue }
    }

    private var serviceCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("服务地址", systemImage: "server.rack")
                .font(.headline)
                .foregroundStyle(.white)

            TextField("https://example.com", text: $originDraft)
                .textInputAutocapitalization(.never)
                .keyboardType(.URL)
                .autocorrectionDisabled()
                .padding(14)
                .background(FootballLiveStyle.background, in: RoundedRectangle(cornerRadius: 12))
                .accessibilityIdentifier("service-origin-field")

            Button(action: saveOrigin) {
                HStack {
                    Text("保存服务地址")
                    Spacer()
                    Image(systemName: "arrow.right")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(AccentButtonStyle())
            .accessibilityIdentifier("save-service-origin")

            if let saveMessage {
                Text(saveMessage)
                    .font(.footnote)
                    .foregroundStyle(saveFailed ? FootballLiveStyle.accent : .green)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("service-origin-message")
            }
        }
        .padding(18)
        .background(FootballLiveStyle.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
    }

    private var connectionHelp: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("首次连接说明", systemImage: "lock.shield")
                .font(.headline)
                .foregroundStyle(.white)
            Text("服务通过 HTTPS 提供比赛和直播线路。若连接提示证书不受信任，请先安装项目提供的公共迁移 CA，并在 iPhone 的“设置 → 通用 → 关于本机 → 证书信任设置”中开启完全信任。")
                .font(.footnote)
                .foregroundStyle(FootballLiveStyle.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(FootballLiveStyle.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
    }

    private func saveOrigin() {
        do {
            try settings.saveOrigin(originDraft)
            saveFailed = false
            saveMessage = "服务地址已保存。"
        } catch {
            saveFailed = true
            saveMessage = "无法保存：\(error.localizedDescription)"
        }
    }
}
