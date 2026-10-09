import SwiftUI

enum HomeSectionPagerLayout {
    static let rowCount = 2
    static let maximumColumnCount = 5
    static let minimumCardWidth: CGFloat = 100
    static let spacing: CGFloat = 14

    static func columnCount(for availableWidth: CGFloat) -> Int {
        let width = max(availableWidth, 1)
        let fitting = Int((width + spacing) / (minimumCardWidth + spacing))
        return min(maximumColumnCount, max(1, fitting))
    }

    static func displayTitle(for title: String) -> String {
        title == "最新上映" ? "最近更新" : title
    }

    static func pages(for items: [MediaItem], columns: Int) -> [[MediaItem?]] {
        guard !items.isEmpty else { return [] }
        let pageSize = max(1, columns) * rowCount
        return stride(from: 0, to: items.count, by: pageSize).map { start in
            let end = min(start + pageSize, items.count)
            var page: [MediaItem?] = items[start..<end].map { Optional($0) }
            page.append(contentsOf: repeatElement(nil, count: pageSize - page.count))
            return page
        }
    }
}

struct HomeSectionPager: View {
    let section: MediaSection
    let viewportWidth: CGFloat

    @State private var currentPage = 0

    private var contentWidth: CGFloat { max(1, viewportWidth - 40) }
    private var columns: Int { HomeSectionPagerLayout.columnCount(for: contentWidth) }
    private var cardWidth: CGFloat {
        (contentWidth - CGFloat(columns - 1) * HomeSectionPagerLayout.spacing) / CGFloat(columns)
    }
    private var posterHeight: CGFloat { cardWidth * 190 / 132 }
    private var cardHeight: CGFloat { posterHeight + 50 }
    private var gridHeight: CGFloat { cardHeight * CGFloat(HomeSectionPagerLayout.rowCount) + 16 }
    private var pages: [[MediaItem?]] {
        HomeSectionPagerLayout.pages(for: section.items, columns: columns)
    }
    private var gridColumns: [GridItem] {
        Array(
            repeating: GridItem(.fixed(cardWidth), spacing: HomeSectionPagerLayout.spacing, alignment: .top),
            count: columns
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack(alignment: .firstTextBaseline) {
                SectionHeading(title: HomeSectionPagerLayout.displayTitle(for: section.title))
                Spacer()
                if pages.count > 1 {
                    Text("\(currentPage + 1) / \(pages.count)")
                        .font(.caption.weight(.medium).monospacedDigit())
                        .foregroundStyle(MyTVStyle.mutedText)
                        .accessibilityIdentifier("home-page-count-\(section.id)")
                }
            }
            .padding(.horizontal, 20)

            if let firstPage = pages.first {
                if pages.count > 1 {
                    TabView(selection: $currentPage) {
                        ForEach(pages.indices, id: \.self) { index in
                            pageGrid(pages[index])
                                .tag(index)
                        }
                    }
                    .tabViewStyle(.page(indexDisplayMode: .never))
                    .frame(width: contentWidth, height: gridHeight)
                    .padding(.horizontal, 20)
                    .accessibilityIdentifier("home-section-pages-\(section.id)")

                    pageIndicators
                } else {
                    pageGrid(firstPage)
                        .frame(width: contentWidth, height: gridHeight)
                        .padding(.horizontal, 20)
                }
            }
        }
        .onChange(of: pages.count) { pageCount in
            currentPage = min(currentPage, max(0, pageCount - 1))
        }
    }

    private func pageGrid(_ items: [MediaItem?]) -> some View {
        LazyVGrid(
            columns: gridColumns,
            alignment: .leading,
            spacing: 16
        ) {
            ForEach(items.indices, id: \.self) { index in
                if let item = items[index] {
                    MediaPosterCard(
                        item: item,
                        width: cardWidth,
                        posterHeight: posterHeight,
                        reserveYearLine: true
                    )
                    .frame(width: cardWidth, height: cardHeight, alignment: .topLeading)
                } else {
                    Color.clear
                        .frame(width: cardWidth, height: cardHeight)
                        .accessibilityHidden(true)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private var pageIndicators: some View {
        HStack(spacing: 6) {
            ForEach(pages.indices, id: \.self) { index in
                Button {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        currentPage = index
                    }
                } label: {
                    Capsule()
                        .fill(index == currentPage ? MyTVStyle.accent : MyTVStyle.mutedText.opacity(0.65))
                        .frame(width: index == currentPage ? 16 : 6, height: 6)
                        .frame(minWidth: 12, minHeight: 20)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("第 \(index + 1) 页")
                .accessibilityIdentifier("home-page-\(section.id)-\(index + 1)")
                .accessibilityAddTraits(index == currentPage ? .isSelected : [])
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 20)
    }
}
