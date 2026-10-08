import SwiftUI

struct SearchView: View {
    @EnvironmentObject private var settings: AppSettings

    @State private var query = ""
    @State private var submittedQuery = ""
    @State private var results: [MediaItem] = []
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var retryToken = 0

    private let columns = [GridItem(.adaptive(minimum: 132), spacing: 14, alignment: .top)]

    var body: some View {
        VStack(spacing: 0) {
            searchHeader
            if submittedQuery.isEmpty {
                ScreenStateView(
                    symbol: "magnifyingglass",
                    title: "搜索影视内容",
                    message: "输入片名后开始搜索。"
                )
                .accessibilityIdentifier("search-empty-state")
            } else if isLoading {
                ScreenStateView(symbol: "magnifyingglass", title: "正在搜索…", message: submittedQuery)
                    .overlay { ProgressView().tint(MyTVStyle.accent).offset(y: 60) }
                    .accessibilityIdentifier("search-loading-state")
            } else if let errorMessage {
                ScreenStateView(
                    symbol: "wifi.exclamationmark",
                    title: "搜索失败",
                    message: errorMessage,
                    actionTitle: "重试",
                    action: { retryToken += 1 }
                )
                .accessibilityIdentifier("search-error-state")
            } else if results.isEmpty {
                ScreenStateView(
                    symbol: "film",
                    title: "没有找到相关内容",
                    message: "试试更短的片名或不同关键词。"
                )
                .accessibilityIdentifier("search-no-results")
            } else {
                ScrollView {
                    LazyVGrid(columns: columns, alignment: .leading, spacing: 20) {
                        ForEach(results) { item in
                            MediaPosterCard(item: item)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(.horizontal, 18)
                    .padding(.vertical, 20)
                }
                .accessibilityIdentifier("search-results")
            }
        }
        .background(MyTVStyle.background)
        .navigationTitle("搜索")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: "\(submittedQuery)-\(retryToken)") {
            guard !submittedQuery.isEmpty else { return }
            await search()
        }
    }

    private var searchHeader: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(MyTVStyle.secondaryText)
            TextField("搜索电影、剧集、动漫", text: $query)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.search)
                .onSubmit { submitSearch() }
                .accessibilityIdentifier("search-query-field")
            if !query.isEmpty {
                Button {
                    query = ""
                    submittedQuery = ""
                    results = []
                    errorMessage = nil
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(MyTVStyle.secondaryText)
                }
                .accessibilityLabel("清除搜索")
            }
            Button("搜索") { submitSearch() }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(MyTVStyle.accent)
                .disabled(query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityIdentifier("search-submit")
        }
        .padding(13)
        .background(MyTVStyle.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .padding(.horizontal, 18)
        .padding(.top, 14)
    }

    private func submitSearch() {
        submittedQuery = query.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    @MainActor
    private func search() async {
        guard let client = settings.client else { return }
        isLoading = true
        errorMessage = nil
        do {
            let found = try await client.search(query: submittedQuery, page: 1)
            try Task.checkCancellation()
            results = found
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
