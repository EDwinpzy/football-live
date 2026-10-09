#if DEBUG
import CoreFoundation
import CoreGraphics
import Foundation
import ImageIO
import SwiftUI
import UniformTypeIdentifiers

private enum UITestPosterProtocolRegistration {
    static let isRegistered = URLProtocol.registerClass(UITestPosterURLProtocol.self)
}

private enum UITestPlaybackProtocolRegistration {
    static let isRegistered = URLProtocol.registerClass(UITestPlaybackURLProtocol.self)
}

private final class UITestPosterURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool {
        guard let url = request.url else { return false }
        return url.scheme?.lowercased() == "https"
            && url.host?.lowercased() == "poster.fixture.invalid"
            && url.pathExtension.lowercased() == "png"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url,
              let response = HTTPURLResponse(
                  url: url,
                  statusCode: 200,
                  httpVersion: "HTTP/1.1",
                  headerFields: ["Content-Type": "image/png", "Cache-Control": "no-store"]
              ) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }

        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: UITestPosterPNG.data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

private final class UITestPlaybackURLProtocol: URLProtocol {
    private static let requestCounter = UITestPlaybackRequestCounter()

    override class func canInit(with request: URLRequest) -> Bool {
        guard let url = request.url else { return false }
        return url.scheme?.lowercased() == "https"
            && url.host?.lowercased() == "playback.fixture.invalid"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }

        let statusCode: Int
        let body: Data
        if url.path.contains("/web/vod/detail/") {
            statusCode = 200
            body = Self.detailResponse(for: url)
        } else if url.path.contains("/web/vod/play/") {
            statusCode = Self.requestCounter.nextFailureStatus(for: url)
            body = Data(#"{"error":"UI fixture intentionally simulates a playback resolution failure."}"#.utf8)
        } else {
            statusCode = 404
            body = Data(#"{"error":"No UI fixture route is registered."}"#.utf8)
        }

        guard let response = HTTPURLResponse(
            url: url,
            statusCode: statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json", "Cache-Control": "no-store"]
        ) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func detailResponse(for url: URL) -> Data {
        let alternateSource = url.path.contains("coast-north")
            ? #",{"id":"fixture-source-2","name":"备用线路","episodes":[{"name":"第 1 集"},{"name":"第 2 集"},{"name":"第 3 集"}]}"#
            : ""
        let contentID = url.path.contains("midnight-letter") ? "midnight-letter" : "coast-north"
        let json = #"{"id":"\#(contentID)","title":"Fixture","summary":"本地 UI 测试详情","sources":[{"id":"fixture-source","name":"高清线路","episodes":[{"name":"第 1 集"},{"name":"第 2 集"},{"name":"第 3 集"}]}\#(alternateSource)]}"#
        return Data(json.utf8)
    }
}

private final class UITestPlaybackRequestCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var playRequestCounts: [String: Int] = [:]

    func nextFailureStatus(for url: URL) -> Int {
        lock.lock()
        defer { lock.unlock() }
        let contentID = url.path.contains("midnight-letter") ? "midnight-letter" : "coast-north"
        let count = (playRequestCounts[contentID] ?? 0) + 1
        playRequestCounts[contentID] = count
        return count == 1 ? 503 : 502
    }
}

private enum UITestPosterPNG {
    static let data: Data = {
        let width = 1_200
        let height = 1_800
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        guard let context = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            return Data()
        }

        context.setFillColor(CGColor(red: 0.07, green: 0.12, blue: 0.34, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        context.setFillColor(CGColor(red: 0.36, green: 0.12, blue: 0.42, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: width, height: 620))
        context.setFillColor(CGColor(red: 0.98, green: 0.27, blue: 0.31, alpha: 1))
        context.fillEllipse(in: CGRect(x: 250, y: 690, width: 700, height: 700))
        context.setFillColor(CGColor(red: 0.22, green: 0.78, blue: 0.88, alpha: 0.9))
        context.fillEllipse(in: CGRect(x: 590, y: 830, width: 400, height: 400))
        context.setFillColor(CGColor(red: 1, green: 0.86, blue: 0.52, alpha: 1))
        for point in [CGPoint(x: 180, y: 1_480), CGPoint(x: 990, y: 1_520), CGPoint(x: 1_020, y: 380)] {
            context.fillEllipse(in: CGRect(x: point.x, y: point.y, width: 36, height: 36))
        }

        guard let image = context.makeImage() else { return Data() }
        let output = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(
            output,
            UTType.png.identifier as CFString,
            1,
            nil
        ) else {
            return Data()
        }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { return Data() }
        return output as Data
    }()
}

enum UITestFixtures {
    static let featuredMovie = MediaItem(
        id: "fixture-movie-1",
        title: "海岸线以北",
        poster: posterURL(for: "fixture-movie-1"),
        detailRef: "fixture:coast-north",
        rating: "8.6",
        year: "2025"
    )

    static let secondMovie = MediaItem(
        id: "fixture-movie-2",
        title: "午夜来信",
        poster: posterURL(for: "fixture-movie-2"),
        detailRef: "fixture:midnight-letter",
        rating: "8.1",
        year: "2024"
    )

    static let series = MediaItem(
        id: "fixture-series-1",
        title: "城中故事",
        poster: posterURL(for: "fixture-series-1"),
        detailRef: "fixture:city-stories",
        rating: "9.0",
        year: "2025"
    )

    static let anime = MediaItem(
        id: "fixture-anime-1",
        title: "星际邮差",
        poster: posterURL(for: "fixture-anime-1"),
        detailRef: "fixture:space-postman",
        rating: "8.8",
        year: "2024"
    )

    static let variety = MediaItem(
        id: "fixture-variety-1",
        title: "周末出发吧",
        poster: posterURL(for: "fixture-variety-1"),
        detailRef: "fixture:weekend-go",
        rating: "8.2",
        year: "2025"
    )

    static let homeMovies = (1...16).map { index in
        if index == 1 { return featuredMovie }
        if index == 2 { return secondMovie }
        return MediaItem(
            id: "fixture-movie-\(index)",
            title: "第\(index)部样例影片",
            poster: posterURL(for: "fixture-movie-\(index)"),
            detailRef: "fixture:movie-\(index)",
            rating: String(format: "%.1f", 7.0 + Double(index % 20) / 10),
            year: "202\(index % 6)"
        )
    }

    static let homeSections = [
        MediaSection(id: "fixture-featured", title: "最近热门", items: homeMovies),
        MediaSection(id: "fixture-series", title: "热播剧集", items: [series]),
        MediaSection(id: "fixture-anime", title: "人气动漫", items: [anime]),
        MediaSection(id: "fixture-variety", title: "精选综艺", items: [variety])
    ]

    private static func posterURL(for id: String) -> String {
        "https://poster.fixture.invalid/\(id).png"
    }

    static func detail(for item: MediaItem) -> MediaDetail {
        let episodes = [
            Episode(id: "fixture-episode-1", name: "第 1 集"),
            Episode(id: "fixture-episode-2", name: "第 2 集"),
            Episode(id: "fixture-episode-3", name: "第 3 集")
        ]
        let source = MediaSource(
            id: "fixture-source",
            name: "高清线路",
            episodes: episodes
        )
        let sources = item.id == featuredMovie.id
            ? [source, MediaSource(id: "fixture-source-2", name: "备用线路", episodes: episodes)]
            : [source]
        return MediaDetail(
            item: item,
            summary: "沿着海岸线向北，一群久别重逢的朋友重新面对旧日选择，也在日常的旅程中找回彼此。",
            sources: sources
        )
    }
}

@MainActor
struct UITestFixtureRootView: View {
    @EnvironmentObject private var license: LicenseStore
    @StateObject private var fixtureSettings: AppSettings

    init() {
        precondition(ProcessInfo.processInfo.arguments.contains("--ui-test-fixtures"), "Fixture mode requires the existing UI test launch flag.")
        precondition(UITestPosterProtocolRegistration.isRegistered, "The fixture poster URL protocol must be registered.")
        precondition(UITestPlaybackProtocolRegistration.isRegistered, "The fixture playback URL protocol must be registered.")

        let defaults = UserDefaults(suiteName: "com.qiubo.mytv.ios.ui-test-fixtures")!
        let settings = AppSettings(defaults: defaults)
        try! settings.saveOrigin("https://playback.fixture.invalid")
        _fixtureSettings = StateObject(wrappedValue: settings)
    }

    var body: some View {
        NavigationStack {
            HomeView(fixtureSections: UITestFixtures.homeSections)
                .installMyTVDestinations(fixtureDetail: UITestFixtures.detail(for:))
        }
        .environmentObject(fixtureSettings)
        .task {
            await MainActor.run {
                license.enableUITestPlaybackAccessIfRequested()
            }
        }
        .myTVScreen()
    }
}
#endif
