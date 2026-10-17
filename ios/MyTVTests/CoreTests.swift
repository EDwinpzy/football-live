import CryptoKit
import CoreGraphics
import CoreFoundation
import Foundation
import ImageIO
import XCTest
import UniformTypeIdentifiers
@testable import MyTV

final class CoreTests: XCTestCase {
    func testHomeParsesBackendSectionAndBuildsDoubanReference() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            let body = #"{"category":"movie","sections":[{"title":"热门电影","items":[{"id":"douban:37068446","title":"示例影片","poster_url":"https://img3.doubanio.com/view/photo/s_ratio_poster/public/p123.jpg","rating":8.7,"year":"2024","release_date":"2024-06-01"}]}],"source":"douban","stale":false}"#
            return (200, Data(body.utf8))
        }
        let client = makeClient()

        let sections = try await client.home(category: "movie")

        XCTAssertEqual(sections.count, 1)
        XCTAssertEqual(sections[0].title, "热门电影")
        XCTAssertEqual(sections[0].items.first?.id, "douban:37068446")
        XCTAssertEqual(sections[0].items.first?.detailRef, "37068446")
        XCTAssertEqual(sections[0].items.first?.rating, "8.7")
        XCTAssertEqual(requests.all.first?.url?.path, "/web/vod/home")
        let requestURL = try XCTUnwrap(requests.all.first?.url)
        XCTAssertEqual(URLComponents(url: requestURL, resolvingAgainstBaseURL: false)?.queryItems?.first?.value, "movie")
    }

    func testHomeRequestsTheBackendCategoryForTVAnd3DChineseAnimation() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            return (200, Data(#"{"sections":[]}"#.utf8))
        }
        let client = makeClient()

        for category in ["tv", "anime"] {
            _ = try await client.home(category: category)
        }

        let categories = requests.all.compactMap { request -> String? in
            guard let url = request.url,
                  url.path == "/web/vod/home" else { return nil }
            return URLComponents(url: url, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "category" })?.value
        }
        XCTAssertEqual(categories, ["tv", "anime"])
    }

    func testSearchPreservesPaginationMetadataAndRequestsTheRequestedPage() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            let body = #"{"query":"海边的曼彻斯特","page":2,"limit":30,"has_more":true,"items":[{"id":"douban:1292052","title":"海边的曼彻斯特"}]}"#
            return (200, Data(body.utf8))
        }
        let client = makeClient()

        let page = try await client.search(query: "海边的曼彻斯特", page: 2)

        XCTAssertEqual(page.page, 2)
        XCTAssertTrue(page.hasMore)
        XCTAssertEqual(page.items.map(\.id), ["douban:1292052"])
        let requestURL = try XCTUnwrap(requests.all.first?.url)
        let query = URLComponents(url: requestURL, resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(query?.first(where: { $0.name == "page" })?.value, "2")
        XCTAssertEqual(query?.first(where: { $0.name == "q" })?.value, "海边的曼彻斯特")
    }

    func testHomeSectionPagerCreatesTwoRowPagesAndPadsTheLastPage() {
        let items = (1...7).map { index in
            MediaItem(id: "douban:\(index)", title: "作品\(index)")
        }

        let pages = HomeSectionPagerLayout.pages(for: items, columns: 3)

        XCTAssertEqual(pages.count, 2)
        XCTAssertEqual(pages[0].count, 6)
        XCTAssertEqual(pages[0].compactMap { $0?.id }, items.prefix(6).map(\.id))
        XCTAssertEqual(pages[1].count, 6)
        XCTAssertEqual(pages[1].compactMap { $0?.id }, ["douban:7"])
        XCTAssertEqual(pages[1].filter { $0 == nil }.count, 5)
    }

    func testHomeSectionPagerAdaptsColumnsToAvailableWidth() {
        XCTAssertEqual(HomeSectionPagerLayout.columnCount(for: 350), 3)
        XCTAssertEqual(HomeSectionPagerLayout.columnCount(for: 510), 4)
        XCTAssertEqual(HomeSectionPagerLayout.columnCount(for: 900), HomeSectionPagerLayout.maximumColumnCount)
        XCTAssertEqual(HomeSectionPagerLayout.columnCount(for: 1), 1)
    }

    func testHomeSectionPagerNormalizesLatestReleaseHeading() {
        XCTAssertEqual(HomeSectionPagerLayout.displayTitle(for: "最新上映"), "最近更新")
        XCTAssertEqual(HomeSectionPagerLayout.displayTitle(for: "最近热门"), "最近热门")
        XCTAssertEqual(HomeSectionPagerLayout.displayTitle(for: "豆瓣高分"), "豆瓣高分")
    }

    func testPosterDecoderDownsamplesToTheRequestedPixelBound() throws {
        let context = try XCTUnwrap(CGContext(
            data: nil,
            width: 1_200,
            height: 1_800,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ))
        context.setFillColor(CGColor(red: 0.9, green: 0.2, blue: 0.2, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: 1_200, height: 1_800))
        let sourceImage = try XCTUnwrap(context.makeImage())

        let encoded = NSMutableData()
        let destination = try XCTUnwrap(CGImageDestinationCreateWithData(
            encoded,
            UTType.png.identifier as CFString,
            1,
            nil
        ))
        CGImageDestinationAddImage(destination, sourceImage, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))

        let thumbnail = try XCTUnwrap(PosterImageDecoder.downsample(
            data: encoded as Data,
            maximumPixelSize: 600
        ))

        XCTAssertEqual(thumbnail.width, 400)
        XCTAssertEqual(thumbnail.height, 600)
    }

    func testHHKanDetailUsesPrefixedIdentityAndParsesPIDVIDPlaybackKeys() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            let body = #"{"id":4422228,"title":"港剧示例","cover":"http://img.example.test/poster.jpg","score":8.1,"desc":"剧情简介","sources":[{"name":"线路一","episodes":[{"ep":"第1集","pid":41,"vid":4422228}]}]}"#
            return (200, Data(body.utf8))
        }
        let client = makeClient()
        let item = MediaItem(id: "hhkan:4422228", title: "港剧示例", detailRef: "4422228")

        let detail = try await client.detail(item: item)

        XCTAssertEqual(requests.all.first?.url?.path, "/web/hhkan/detail/4422228")
        XCTAssertEqual(detail.item.id, "hhkan:4422228")
        XCTAssertEqual(detail.item.detailRef, "hhkan:4422228")
        XCTAssertEqual(detail.sources.first?.id, "41")
        XCTAssertEqual(detail.sources.first?.episodes.first?.id, "4422228")
    }

    func testHHKanDetailRefreshRequestsLatestEnrichedEpisodeSources() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            return (200, Data(#"{"id":4422228,"title":"港剧示例","sources":[]}"#.utf8))
        }
        let client = makeClient()
        let item = MediaItem(id: "hhkan:4422228", title: "港剧示例", detailRef: "hhkan:4422228")

        _ = try await client.detail(item: item, refresh: true)

        let requestURL = try XCTUnwrap(requests.all.first?.url)
        XCTAssertEqual(requestURL.path, "/web/hhkan/detail/4422228")
        let queryItems = URLComponents(url: requestURL, resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(queryItems?.first(where: { $0.name == "refresh" })?.value, "1")
        XCTAssertEqual(queryItems?.first(where: { $0.name == "enrich" })?.value, "1")
    }

    func testPlaybackSourceSelectionPreservesSourceAndEpisodeAcrossRefresh() throws {
        let original = MediaSource(
            id: "line-a",
            name: "高清线路",
            episodes: [
                Episode(id: "a1", name: "第 1 集"),
                Episode(id: "a2", name: "第 2 集"),
                Episode(id: "a3", name: "第 3 集")
            ]
        )
        let reorderedRefresh = MediaSource(
            id: "line-a",
            name: "高清线路",
            episodes: [
                Episode(id: "fresh1", name: "第 1 集"),
                Episode(id: "fresh2", name: "第 2 集"),
                Episode(id: "fresh3", name: "第 3 集")
            ]
        )
        let secondLine = MediaSource(
            id: "line-b",
            name: "备用线路",
            episodes: [
                Episode(id: "b1", name: "第 1 集"),
                Episode(id: "b2", name: "第 2 集")
            ]
        )

        let selected = try XCTUnwrap(PlaybackSourceSelection.refreshedSource(
            matching: original,
            in: [secondLine, reorderedRefresh]
        ))
        let refreshedEpisode = PlaybackSourceSelection.refreshedEpisode(
            in: selected,
            matching: original.episodes[1]
        )
        let otherLineEpisode = PlaybackSourceSelection.episode(
            in: secondLine,
            matching: original.episodes[2],
            from: original
        )

        XCTAssertEqual(selected.id, "line-a")
        let renamedID = MediaSource(id: "line-a-refreshed", name: " 高清线路 ", episodes: reorderedRefresh.episodes)
        XCTAssertEqual(
            PlaybackSourceSelection.refreshedSource(matching: original, in: [renamedID])?.id,
            "line-a-refreshed",
            "Line names preserve a selected source when a fresh detail response changes its identifier."
        )
        XCTAssertNil(
            PlaybackSourceSelection.refreshedSource(
                matching: MediaSource(id: "old-id", name: "Removed line", episodes: []),
                in: [secondLine, reorderedRefresh]
            ),
            "A refresh must not silently redirect a hand-selected source to a different line."
        )
        XCTAssertEqual(refreshedEpisode?.id, "fresh2")
        XCTAssertEqual(otherLineEpisode?.id, "b2", "When a line has fewer episodes, keep the nearest available episode position.")
        XCTAssertEqual(
            PlaybackSourceSelection.episode(in: original, matching: original.episodes[1], from: original)?.id,
            "a2",
            "Reselecting the current source keeps the same episode so the tap can retry playback."
        )
    }

    func testRememberedVODSourcePersistsPerTitleAndMovesToTheFrontWhenAvailable() async throws {
        let suiteName = "MyTVTests.VODSourcePreference.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let savedSource = MediaSource(id: "line-b", name: "备用线路", episodes: [])
        let settings = await MainActor.run { AppSettings(defaults: defaults) }
        await MainActor.run {
            settings.rememberVODSource(savedSource, for: "douban:1292052")
        }

        let reopenedSettings = await MainActor.run { AppSettings(defaults: defaults) }
        let preference = await MainActor.run {
            reopenedSettings.rememberedVODSource(for: "douban:1292052")
        }
        let otherTitlePreference = await MainActor.run {
            reopenedSettings.rememberedVODSource(for: "douban:another-title")
        }
        let availableSources = [
            MediaSource(id: "line-a", name: "高清线路", episodes: []),
            MediaSource(id: "line-b-refreshed", name: "备用线路", episodes: [])
        ]
        let ordered = PlaybackSourceSelection.ordered(availableSources, preferring: preference)

        XCTAssertEqual(preference, PlaybackSourcePreference(id: "line-b", name: "备用线路"))
        XCTAssertEqual(ordered.map(\.name), ["备用线路", "高清线路"])
        XCTAssertNil(otherTitlePreference)
        XCTAssertEqual(
            PlaybackSourceSelection.ordered(availableSources, preferring: preference).first?.id,
            "line-b-refreshed",
            "A renamed source ID is still recognized by its unique name."
        )
    }

    func testVODDetailAndPlayUseFiveMinuteTimeoutWhileHomeKeepsDefaultTimeout() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            switch request.url?.path {
            case "/web/vod/home":
                return (200, Data(#"{"sections":[]}"#.utf8))
            case "/web/vod/detail/1292052":
                return (200, Data(#"{"id":1292052,"title":"示例影片","sources":[{"id":"12","name":"线路","episodes":[{"id":"0","name":"正片"}]}]}"#.utf8))
            default:
                return (200, Data(#"{"url":"https://cdn.example.test/vod.m3u8"}"#.utf8))
            }
        }
        let client = makeClient()
        let item = MediaItem(id: "douban:1292052", title: "示例影片", detailRef: "1292052")

        _ = try await client.home(category: "movie")
        let detail = try await client.detail(item: item, refresh: true)
        let source = try XCTUnwrap(detail.sources.first)
        _ = try await client.play(
            detailRef: item.detailRef,
            source: source,
            episode: try XCTUnwrap(source.episodes.first)
        )

        XCTAssertEqual(requests.all.map(\.timeoutInterval), [30, 300, 300])
        XCTAssertEqual(PlaybackTimeout.vodMilliseconds, 300_000)
        XCTAssertEqual(PlaybackTimeout.vodSeconds, 300, accuracy: 0.001)
    }

    func testFreshEpisodeSelectionUsesUniqueNameWhenBackendReusesReorderedIDs() {
        let selectedEpisode = Episode(id: "1", name: "第 2 集")
        let refreshed = MediaSource(
            id: "line-a",
            name: "高清线路",
            episodes: [
                Episode(id: "0", name: "第 2 集"),
                Episode(id: "1", name: "第 1 集")
            ]
        )

        let result = PlaybackSourceSelection.refreshedEpisode(in: refreshed, matching: selectedEpisode)

        XCTAssertEqual(result?.id, "0", "A reused positional ID must not override the unique matching episode name.")
        XCTAssertEqual(result?.name, "第 2 集")
    }

    func testFreshEpisodeSelectionRejectsRemovedAndDuplicateEpisodeNames() {
        let removedEpisode = Episode(id: "1", name: "第 2 集")
        let refreshedWithoutSelection = MediaSource(
            id: "line-a",
            name: "高清线路",
            episodes: [Episode(id: "1", name: "第 1 集")]
        )
        XCTAssertNil(
            PlaybackSourceSelection.refreshedEpisode(in: refreshedWithoutSelection, matching: removedEpisode),
            "A reused ID cannot make a removed episode look present."
        )

        let duplicateNameSource = MediaSource(
            id: "line-a",
            name: "高清线路",
            episodes: [
                Episode(id: "1", name: "第 2 集"),
                Episode(id: "b", name: "第 2 集")
            ]
        )
        XCTAssertNil(
            PlaybackSourceSelection.refreshedEpisode(in: duplicateNameSource, matching: removedEpisode),
            "Duplicate episode names are ambiguous even if a backend ID happens to match."
        )

        let unnamedRefresh = MediaSource(
            id: "line-a",
            name: "高清线路",
            episodes: [Episode(id: "1", name: "")]
        )
        XCTAssertEqual(
            PlaybackSourceSelection.refreshedEpisode(in: unnamedRefresh, matching: removedEpisode)?.id,
            "1",
            "A unique ID may be used only when the refreshed source has no credible episode names."
        )
    }

    func testFreshSourceSelectionRejectsDuplicateNameFallbacks() {
        let selected = MediaSource(id: "removed-id", name: "高清线路", episodes: [])
        let refreshedSources = [
            MediaSource(id: "new-a", name: "高清线路", episodes: []),
            MediaSource(id: "new-b", name: " 高清线路 ", episodes: [])
        ]

        XCTAssertNil(
            PlaybackSourceSelection.refreshedSource(matching: selected, in: refreshedSources),
            "The app must not silently choose one of multiple sources with the same display name."
        )
    }

    func testPlaybackRouteCarriesAllSourcesAndKeepsTheSingleSourceForRetry() {
        let item = MediaItem(id: "douban:1292052", title: "示例影片", detailRef: "1292052")
        let source = MediaSource(
            id: "line-only",
            name: "唯一线路",
            episodes: [Episode(id: "episode-1", name: "正片")]
        )
        let backupSource = MediaSource(
            id: "line-backup",
            name: "备用线路",
            episodes: [Episode(id: "backup-episode-1", name: "正片")]
        )
        let episode = source.episodes[0]

        let multiSourceRoute = PlaybackRoute(
            item: item,
            detailRef: item.detailRef,
            source: source,
            episode: episode,
            availableSources: [source, backupSource]
        )
        let singleSourceRoute = PlaybackRoute(item: item, detailRef: item.detailRef, source: source, episode: episode)

        XCTAssertEqual(multiSourceRoute.availableSources, [source, backupSource])
        XCTAssertEqual(singleSourceRoute.availableSources, [source])
    }

    func testPlaybackRetryTargetKeepsTheSelectedSourceEpisodeAndSeekPosition() {
        let source = MediaSource(id: "line-a", name: "高清线路", episodes: [])
        let episode = Episode(id: "episode-3", name: "第 3 集")

        let retry = PlaybackRetryPolicy.target(source: source, episode: episode, resumeTime: 1_245)

        XCTAssertEqual(retry?.source, source)
        XCTAssertEqual(retry?.episode, episode)
        XCTAssertEqual(retry?.resumeTime, 1_245)
        XCTAssertNil(PlaybackRetryPolicy.target(source: nil, episode: episode, resumeTime: 1_245))
        XCTAssertNil(PlaybackRetryPolicy.target(source: source, episode: nil, resumeTime: 1_245))
        XCTAssertEqual(PlaybackRetryPolicy.safeTime(.nan, fallback: 1_245), 1_245)
        XCTAssertEqual(PlaybackRetryPolicy.safeTime(-1, fallback: 1_245), 1_245)
        XCTAssertEqual(
            PlaybackRetryPolicy.resumeTime(playerTime: 0, pendingSeekTarget: 1_245, savedPlaybackTime: 1_245),
            1_245,
            "A failed seek must retry from its requested target even before AVPlayer reports that position."
        )
        XCTAssertEqual(
            PlaybackRetryPolicy.resumeTime(playerTime: 1_280, pendingSeekTarget: nil, savedPlaybackTime: 1_245),
            1_280,
            "After seek completion, the current playback position takes over as the retry point."
        )
    }

    func testPlaybackFailureFallsBackToSameLineRelayBeforeRetryAndKeepsLivePolicy() throws {
        let episode = Episode(id: "episode-3", name: "第 3 集")
        let source = MediaSource(id: "line-a", name: "高清线路", episodes: [episode])
        let relayURL = try XCTUnwrap(URL(string: "https://relay.example.invalid/play.m3u8"))

        XCTAssertEqual(
            PlaybackRetryPolicy.failureAction(
                isVOD: true,
                isDirectAttempt: true,
                relayURL: relayURL,
                source: source,
                episode: episode,
                resumeTime: 1_245
            ),
            .relay(relayURL),
            "A failed direct request should first reuse its same-line relay URL."
        )
        XCTAssertEqual(
            PlaybackRetryPolicy.failureAction(
                isVOD: true,
                isDirectAttempt: false,
                relayURL: nil,
                source: source,
                episode: episode,
                resumeTime: 1_245
            ),
            .retry(PlaybackRetryTarget(source: source, episode: episode, resumeTime: 1_245)),
            "A relay failure should request a fresh stream for the same line and episode."
        )
        XCTAssertEqual(
            PlaybackRetryPolicy.failureAction(
                isVOD: false,
                isDirectAttempt: true,
                relayURL: relayURL,
                source: source,
                episode: episode,
                resumeTime: 1_245
            ),
            .showFailure,
            "Live playback should keep its existing failure handling."
        )
    }

    func testLegacyNumericDetailRefsKeepDoubanAndHHKanRoutesDistinct() {
        let oldDouban = BackendClient.detailRoute(detailRef: "1292052", itemID: "douban:1292052")
        let oldHHKan = BackendClient.detailRoute(detailRef: "4422228", itemID: "hhkan:4422228")

        XCTAssertEqual(oldDouban.segments, ["vod", "detail", "1292052"])
        XCTAssertFalse(oldDouban.isHHKan)
        XCTAssertEqual(oldHHKan.segments, ["hhkan", "detail", "4422228"])
        XCTAssertTrue(oldHHKan.isHHKan)
    }

    func testEndpointPathEncodesEachDynamicSegment() throws {
        let client = makeClient()

        let url = try client.endpointURL(segments: ["vod", "detail", "a/b ?#"])

        XCTAssertEqual(URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedPath, "/web/vod/detail/a%2Fb%20%3F%23")
    }

    func testFootballDiscoversOnlyResolvedM3U8SourcesForEachMatchOpen() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            if request.url?.path == "/web/api/matches" {
                let body = #"{"count":1,"matches":[{"match_id":"9001","title":"甲队 vs 乙队","home":"甲队","away":"乙队","league":"联赛","date":"2026-10-09","time":"20:00","status":"live","score":{"home":1,"away":0},"channels":[{"id":"8121","src":"a2","name":"旧名称A2"}]}]}"#
                return (200, Data(body.utf8))
            }
            XCTAssertEqual(request.url?.path, "/web/api/stream/9001")
            let body = #"{"url":"https://stream.example.test/first.m3u8","source":"88z_2","name":"英超-线路 Q2","referer":"https://embed.example.test/premier?id=2","sources":[{"url":"https://stream.example.test/first.m3u8","player":"m3u8","source":"88z_2","name":"英超-线路 Q2","referer":"https://embed.example.test/premier?id=2"},{"url":"http://cdn.example.test/live.m3u8?token=a+b","player":"m3u8","source":"88z_11","name":"法甲-线路 Q11","referer":"https://embed.example.test/ligue1"},{"url":"https://player.example.test/embed/2","player":"iframe","source":"88z_12","name":"网页线路","referer":"https://embed.example.test/other"},{"url":"not a url","player":"m3u8","source":"88z_13","name":"损坏地址"},{"url":"https://cdn.example.test/no-id.m3u8","player":"m3u8","source":"","name":"缺少线路 ID"}]}"#
            return (200, Data(body.utf8))
        }
        let client = makeClient()
        let matches = try await client.matches()
        let match = try XCTUnwrap(matches.first)

        XCTAssertEqual(match.id, "9001")
        let streams = try await client.matchStreams(match: match)

        XCTAssertEqual(streams.map(\.id), ["88z_2", "88z_11"])
        XCTAssertEqual(streams.map(\.src), ["88z_2", "88z_11"])
        XCTAssertEqual(streams.map(\.name), ["英超-线路 Q2", "法甲-线路 Q11"])
        XCTAssertEqual(streams.map(\.player), ["m3u8", "m3u8"])
        XCTAssertEqual(streams.map(\.referer), ["https://embed.example.test/premier?id=2", "https://embed.example.test/ligue1"])
        XCTAssertEqual(streams.map { $0.url.host }, ["stream.example.test", "cdn.example.test"])
        let streamRequest = try XCTUnwrap(requests.all.first { $0.url?.path == "/web/api/stream/9001" })
        let query = URLComponents(url: try XCTUnwrap(streamRequest.url), resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(query?.first(where: { $0.name == "src" })?.value, "all")
        XCTAssertEqual(query?.first(where: { $0.name == "fresh" })?.value, "1")
    }

    func testFootballMatchRecognizesCompletedStatusVariants() {
        let completedMatches = ["finished", "ended", "close", " Finished "].map { status in
            FootballMatch(
                id: status,
                title: "甲队 vs 乙队",
                home: "甲队",
                away: "乙队",
                league: "联赛",
                time: "20:00",
                status: status
            )
        }
        XCTAssertTrue(completedMatches.allSatisfy(\.isFinished))

        let upcoming = FootballMatch(
            id: "upcoming",
            title: "甲队 vs 乙队",
            home: "甲队",
            away: "乙队",
            league: "联赛",
            time: "20:00",
            status: "upcoming"
        )
        XCTAssertFalse(upcoming.isFinished)
        XCTAssertEqual((completedMatches + [upcoming]).filter { !$0.isFinished }.map(\.id), ["upcoming"])
    }

    func testFootballFreshMatchUsesBeijingDateAndLeavesFinishDecisionToBackend() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            let body = #"{"count":1,"matches":[{"match_id":"year-boundary","home":"甲队","away":"乙队","league":"英超","date":"12-31","date_full":"2026-12-31","time":"23:50","status":"upcoming"}]}"#
            return (200, Data(body.utf8))
        }
        let client = makeClient()
        let matches = try await client.matches(fresh: true)
        let match = try XCTUnwrap(matches.first)
        let kickoff = try XCTUnwrap(ISO8601DateFormatter().date(from: "2026-12-31T23:50:00+08:00"))

        XCTAssertEqual(match.kickoffAt, kickoff)
        XCTAssertEqual(match.projected(at: kickoff.addingTimeInterval(149 * 60)).status, "live")
        XCTAssertEqual(match.projected(at: kickoff.addingTimeInterval(150 * 60)).status, "upcoming")

        var sourceLive = match
        sourceLive.status = "live"
        XCTAssertEqual(sourceLive.projected(at: kickoff.addingTimeInterval(180 * 60)).status, "live")
        var postponed = match
        postponed.status = "postponed"
        XCTAssertEqual(postponed.projected(at: kickoff.addingTimeInterval(240 * 60)).status, "postponed")
        XCTAssertEqual(postponed.displayStatus, "延期")

        let request = try XCTUnwrap(requests.all.first)
        let query = URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(query?.first(where: { $0.name == "fresh" })?.value, "1")
    }

    func testFootballRenewsSelectedSourceByMatchAndPreservesItsReferer() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            let body = #"{"url":"https://stream.example.test/live/index.m3u8?token=a+b&x=1","source":"88z_7","name":"Premier League - Channel 7","referer":"https://embed.example.test/watch?id=1&source=88z_7"}"#
            return (200, Data(body.utf8))
        }
        let client = makeClient()
        let match = FootballMatch(
            id: "9001", title: "甲队 vs 乙队", home: "甲队", away: "乙队", league: "英超",
            time: "20:00", status: "live"
        )
        let source = FootballStreamReference(
            id: "88z_7", src: "88z_7", name: "Premier League - Channel 7", player: "m3u8",
            url: URL(string: "https://stream.example.test/old.m3u8")!,
            referer: "https://embed.example.test/old"
        )

        let stream = try await client.matchStream(match: match, source: source)

        let request = try XCTUnwrap(requests.all.first)
        XCTAssertEqual(request.url?.path, "/web/api/stream/9001")
        let query = URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(query?.first(where: { $0.name == "src" })?.value, "88z_7")
        XCTAssertEqual(query?.first(where: { $0.name == "fresh" })?.value, "1")
        XCTAssertEqual(stream.url.path, "/web/api/relay")
        let relayComponents = try XCTUnwrap(URLComponents(url: stream.url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(relayComponents.queryItems?.first(where: { $0.name == "ref" })?.value, "https://embed.example.test/watch?id=1&source=88z_7")
        XCTAssertEqual(relayComponents.queryItems?.first(where: { $0.name == "u" })?.value, "https://stream.example.test/live/index.m3u8?token=a+b&x=1")
        XCTAssertTrue(relayComponents.percentEncodedQuery?.contains("a%2Bb%26x%3D1") == true)
        let relayItems = relayComponents.queryItems ?? []
        XCTAssertEqual(relayItems.count, 2)
        XCTAssertEqual(Set(relayItems.map(\.name)), Set(["u", "ref"]))
    }

    func testRelayDoesNotWrapItsOwnURLTwice() throws {
        let client = makeClient()
        let existing = try client.endpointURL(segments: ["api", "relay"], query: [URLQueryItem(name: "u", value: "https://stream.example.test/a.m3u8")])
        let defaultRelay = client.relayURL(for: "https://stream.example.test/a.m3u8")

        XCTAssertEqual(client.relayURL(for: existing.absoluteString), existing)
        XCTAssertNil(URLComponents(url: defaultRelay, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "ref" }))
    }

    func testVodPlayUsesNoRefererAndPreservesPlusAndAmpersandInSourceURL() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            let body = #"{"url":"https://cdn.example.test/vod/playlist.m3u8?token=a+b&part=one"}"#
            return (200, Data(body.utf8))
        }
        let client = makeClient()
        let source = MediaSource(id: "12", name: "线路", episodes: [])
        let episode = Episode(id: "0", name: "正片")

        let stream = try await client.play(detailRef: "1292052", source: source, episode: episode)
        let components = try XCTUnwrap(URLComponents(url: stream.url, resolvingAgainstBaseURL: false))

        XCTAssertEqual(components.path, "/web/api/relay")
        XCTAssertEqual(requests.all.first?.url?.path, "/web/vod/play/1292052/12/0")
        XCTAssertEqual(stream.directURL?.absoluteString, "https://cdn.example.test/vod/playlist.m3u8?token=a+b&part=one")
        XCTAssertEqual(components.queryItems?.first(where: { $0.name == "u" })?.value, "https://cdn.example.test/vod/playlist.m3u8?token=a+b&part=one")
        XCTAssertEqual(components.queryItems?.first(where: { $0.name == "ref" })?.value, "none")
        XCTAssertTrue(components.percentEncodedQuery?.contains("token%3Da%2Bb%26part%3Done") == true)
    }

    func testHHKanPlayUsesNumericContentIDAndSelectedSourceEpisodeKeys() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            let body = #"{"sources":[{"name":"线路一·清晰1","url":"https://cdn.example.test/episode.m3u8"}]}"#
            return (200, Data(body.utf8))
        }
        let client = makeClient()
        let source = MediaSource(id: "41", name: "线路一", episodes: [])
        let episode = Episode(id: "4422228", name: "第1集")

        _ = try await client.play(detailRef: "hhkan:4422228", source: source, episode: episode)

        XCTAssertEqual(requests.all.first?.url?.path, "/web/hhkan/play/4422228/41/4422228")
    }

    func testHHKanPlayCanBypassResolutionCacheForManualRetry() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            return (200, Data(#"{"url":"https://cdn.example.test/episode.m3u8"}"#.utf8))
        }
        let client = makeClient()
        let source = MediaSource(id: "41", name: "线路一", episodes: [])
        let episode = Episode(id: "4422228", name: "第1集")

        _ = try await client.play(detailRef: "hhkan:4422228", source: source, episode: episode, fresh: true)

        let request = try XCTUnwrap(requests.all.first)
        XCTAssertEqual(request.url?.path, "/web/hhkan/play/4422228/41/4422228")
        let queryItems = URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(queryItems?.first(where: { $0.name == "fresh" })?.value, "1")
    }

    func testDoubanDetailRefreshRequestsFreshSourceMetadata() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            return (200, Data(#"{"id":1292052,"title":"示例影片","sources":[{"name":"线路一","episodes":[{"ep":"第1集","id":"e1"}]}]}"#.utf8))
        }
        let client = makeClient()
        let item = MediaItem(id: "douban:1292052", title: "示例影片", detailRef: "1292052")

        _ = try await client.detail(item: item, refresh: true)

        let requestURL = try XCTUnwrap(requests.all.first?.url)
        XCTAssertEqual(requestURL.path, "/web/vod/detail/1292052")
        XCTAssertEqual(URLComponents(url: requestURL, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "refresh" })?.value, "1")
    }

    func testDoubanManualRetryDoesNotUseUnsupportedPlayFreshParameter() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            return (200, Data(#"{"url":"https://cdn.example.test/vod.m3u8"}"#.utf8))
        }
        let client = makeClient()
        let source = MediaSource(id: "12", name: "线路", episodes: [])
        let episode = Episode(id: "0", name: "正片")

        _ = try await client.play(detailRef: "1292052", source: source, episode: episode, fresh: true)

        let requestURL = try XCTUnwrap(requests.all.first?.url)
        XCTAssertEqual(requestURL.path, "/web/vod/play/1292052/12/0")
        XCTAssertNil(URLComponents(url: requestURL, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "fresh" }))
    }

    func testLicenseTicketSignatureSupportsDERAndRawP1363() throws {
        let key = P256.Signing.PrivateKey()
        let code = "OTV-ABCDE-23456"
        let plan = "weekly"
        let days = 7
        let issued = "1791518400"
        let nonce = "nonce-12345678"
        let message = Data("1|\(code)|\(plan)|\(days)|\(issued)|\(nonce)".utf8)
        let signature = try key.signature(for: message)
        let ticket = LicenseTicket(
            v: 1,
            code: code,
            plan: plan,
            days: days,
            issued: issued,
            nonce: nonce,
            sig: signature.derRepresentation.base64EncodedString()
        )

        XCTAssertTrue(LicenseTicketVerifier.verify(ticket, expectedCode: code, publicKeyDER: key.publicKey.derRepresentation))
        XCTAssertFalse(LicenseTicketVerifier.verify(ticket, expectedCode: "OTV-BCDEF-23456", publicKeyDER: key.publicKey.derRepresentation))
        let raw = try LicenseTicketVerifier.rawSignature(fromDER: signature.derRepresentation)
        XCTAssertEqual(raw, signature.rawRepresentation)
        XCTAssertEqual(try LicenseTicketVerifier.derSignature(fromRaw: raw), signature.derRepresentation)

        var rawTicket = ticket
        rawTicket.sig = raw.base64EncodedString()
        XCTAssertTrue(LicenseTicketVerifier.verify(rawTicket, expectedCode: code, publicKeyDER: key.publicKey.derRepresentation))
    }

    func testOfflineLicenseWindowIsBoundedByGraceAndServerExpiry() async {
        let serverNow: Int64 = 1_800_000_000_000
        let localNow: Int64 = 50_000
        let grace: Int64 = 24 * 60 * 60 * 1_000
        let withinGrace = await MainActor.run {
            LicenseStore.offlineTimeAllows(
                expireAt: nil,
                serverNow: serverNow,
                lastVerifiedLocal: localNow,
                nowMilliseconds: localNow + grace
            )
        }
        let beyondGrace = await MainActor.run {
            LicenseStore.offlineTimeAllows(
                expireAt: nil,
                serverNow: serverNow,
                lastVerifiedLocal: localNow,
                nowMilliseconds: localNow + grace + 1
            )
        }
        let expiredAtServer = await MainActor.run {
            LicenseStore.offlineTimeAllows(
                expireAt: serverNow + 100,
                serverNow: serverNow,
                lastVerifiedLocal: localNow,
                nowMilliseconds: localNow + 101
            )
        }
        let clockMovedBackwards = await MainActor.run {
            LicenseStore.offlineTimeAllows(
                expireAt: nil,
                serverNow: serverNow,
                lastVerifiedLocal: localNow,
                nowMilliseconds: localNow - 1
            )
        }

        XCTAssertTrue(withinGrace)
        XCTAssertFalse(beyondGrace)
        XCTAssertFalse(expiredAtServer)
        XCTAssertFalse(clockMovedBackwards)
    }
}

private func makeClient() -> BackendClient {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [StubURLProtocol.self]
    let session = URLSession(configuration: configuration)
    return BackendClient(origin: URL(string: "https://api.example.test")!, session: session)
}

private final class RequestRecorder {
    private let lock = NSLock()
    private var requests: [URLRequest] = []

    func append(_ request: URLRequest) {
        lock.lock()
        requests.append(request)
        lock.unlock()
    }

    var all: [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return requests
    }
}

private final class StubURLProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var currentHandler: ((URLRequest) -> (Int, Data))?

    static func install(handler: @escaping (URLRequest) -> (Int, Data)) {
        lock.lock()
        currentHandler = handler
        lock.unlock()
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lock.lock()
        let handler = Self.currentHandler
        Self.lock.unlock()
        guard let handler,
              let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        let (statusCode, payload) = handler(request)
        guard
              let response = HTTPURLResponse(
                  url: url,
                  statusCode: statusCode,
                  httpVersion: "HTTP/1.1",
                  headerFields: ["Content-Type": "application/json"]
              ) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: payload)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
