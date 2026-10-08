import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var settings: AppSettings
    @EnvironmentObject private var license: LicenseStore

    var isFirstRun = false

    @State private var originDraft = ""
    @State private var activationCode = ""
    @State private var saveMessage: String?
    @State private var saveFailed = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(isFirstRun ? "欢迎使用 My TV" : "设置")
                        .font(.largeTitle.weight(.bold))
                        .foregroundStyle(.white)
                        .accessibilityIdentifier("settings-title")
                    Text(isFirstRun
                         ? "先确认服务地址，再浏览片库。首次使用需要安装并信任 My TV 公共迁移 CA。"
                         : "管理服务连接和自用版激活状态。")
                        .font(.subheadline)
                        .foregroundStyle(MyTVStyle.secondaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }

                serviceCard
                activationCard
                connectionHelp
            }
            .padding(.horizontal, 20)
            .padding(.top, 24)
            .padding(.bottom, 40)
            .frame(maxWidth: 620, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(MyTVStyle.background)
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
                .background(MyTVStyle.background, in: RoundedRectangle(cornerRadius: 12))
                .accessibilityIdentifier("service-origin-field")

            Button {
                saveOrigin()
            } label: {
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
                    .foregroundStyle(saveFailed ? MyTVStyle.accent : .green)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("service-origin-message")
            }
        }
        .padding(18)
        .background(MyTVStyle.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
    }

    private var activationCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label("自用版激活", systemImage: "checkmark.seal.fill")
                    .font(.headline)
                    .foregroundStyle(.white)
                Spacer()
                Text(license.isActive ? "已激活" : "未激活")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(license.isActive ? .green : MyTVStyle.secondaryText)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(MyTVStyle.background, in: Capsule())
                    .accessibilityIdentifier("license-state")
            }

            Text(license.statusText)
                .font(.footnote)
                .foregroundStyle(MyTVStyle.secondaryText)
                .fixedSize(horizontal: false, vertical: true)

            if !license.isActive {
                TextField("输入激活码", text: $activationCode)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    .padding(14)
                    .background(MyTVStyle.background, in: RoundedRectangle(cornerRadius: 12))
                    .accessibilityIdentifier("license-code-field")

                Button {
                    Task {
                        guard let client = settings.client else { return }
                        await license.activate(code: activationCode.trimmingCharacters(in: .whitespacesAndNewlines), client: client)
                    }
                } label: {
                    HStack {
                        if license.busy { ProgressView().tint(.white) }
                        Text(license.busy ? "正在验证…" : "激活")
                        Spacer()
                        Image(systemName: "key.fill")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(AccentButtonStyle())
                .disabled(license.busy || activationCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || settings.client == nil)
                .opacity((license.busy || settings.client == nil) ? 0.65 : 1)
                .accessibilityIdentifier("activate-license")
            }
        }
        .padding(18)
        .background(MyTVStyle.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
    }

    private var connectionHelp: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("首次连接说明", systemImage: "lock.shield")
                .font(.headline)
                .foregroundStyle(.white)
            Text("服务通过 HTTPS 提供内容。若连接提示证书不受信任，请先安装项目提供的 My TV 公共迁移 CA，并在 iPhone 的“设置 → 通用 → 关于本机 → 证书信任设置”中开启完全信任。")
                .font(.footnote)
                .foregroundStyle(MyTVStyle.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(MyTVStyle.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
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
