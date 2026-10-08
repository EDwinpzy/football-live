#if DEBUG
import SwiftUI

enum UITestFixtures {
    static let featuredMovie = MediaItem(
        id: "fixture-movie-1",
        title: "海岸线以北",
        poster: "",
        detailRef: "fixture:coast-north",
        rating: "8.6",
        year: "2025"
    )

    static let secondMovie = MediaItem(
        id: "fixture-movie-2",
        title: "午夜来信",
        poster: "",
        detailRef: "fixture:midnight-letter",
        rating: "8.1",
        year: "2024"
    )

    static let series = MediaItem(
        id: "fixture-series-1",
        title: "城中故事",
        poster: "",
        detailRef: "fixture:city-stories",
        rating: "9.0",
        year: "2025"
    )

    static let anime = MediaItem(
        id: "fixture-anime-1",
        title: "星际邮差",
        poster: "",
        detailRef: "fixture:space-postman",
        rating: "8.8",
        year: "2024"
    )

    static let variety = MediaItem(
        id: "fixture-variety-1",
        title: "周末出发吧",
        poster: "",
        detailRef: "fixture:weekend-go",
        rating: "8.2",
        year: "2025"
    )

    static let homeSections = [
        MediaSection(id: "fixture-featured", title: "为你推荐", items: [featuredMovie, secondMovie]),
        MediaSection(id: "fixture-series", title: "热播剧集", items: [series]),
        MediaSection(id: "fixture-anime", title: "人气动漫", items: [anime]),
        MediaSection(id: "fixture-variety", title: "精选综艺", items: [variety])
    ]

    static func detail(for item: MediaItem) -> MediaDetail {
        let source = MediaSource(
            id: "fixture-source",
            name: "高清线路",
            episodes: [
                Episode(id: "fixture-episode-1", name: "第 1 集"),
                Episode(id: "fixture-episode-2", name: "第 2 集"),
                Episode(id: "fixture-episode-3", name: "第 3 集")
            ]
        )
        return MediaDetail(
            item: item,
            summary: "沿着海岸线向北，一群久别重逢的朋友重新面对旧日选择，也在日常的旅程中找回彼此。",
            sources: [source]
        )
    }
}

struct UITestFixtureRootView: View {
    var body: some View {
        NavigationStack {
            HomeView(fixtureSections: UITestFixtures.homeSections)
                .installMyTVDestinations(fixtureDetail: UITestFixtures.detail(for:))
        }
        .myTVScreen()
    }
}
#endif
