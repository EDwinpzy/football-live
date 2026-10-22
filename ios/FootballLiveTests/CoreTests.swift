import Foundation
import XCTest
@testable import FootballLive

final class CoreTests: XCTestCase {
    func testMatchesParseScoresAndIgnoreMalformedRows() async throws {
        StubURLProtocol.install { request in
            XCTAssertEqual(request.url?.path, "/web/api/matches")
            let body = #"{"matches":[{"match_id":"9001","home":"甲队","away":"乙队","league":"英超","date":"10-22","time":"20:00","status":"live","minute":72,"score":{"home":2,"away":1}},{"match_id":"bad","home":"缺少客队"}]}"#
            return (200, Data(body.utf8))
        }

        let matches = try await makeClient().matches()

        XCTAssertEqual(matches.count, 1)
        XCTAssertEqual(matches.first?.id, "9001")
        XCTAssertEqual(matches.first?.title, "甲队 vs 乙队")
        XCTAssertEqual(matches.first?.time, "10-22 20:00")
        XCTAssertEqual(matches.first?.displayStatus, "直播中")
        XCTAssertEqual(matches.first?.score, "2-1")
        XCTAssertEqual(matches.first?.minute, "72")
    }

    func testFreshMatchRequestAndKickoffProjection() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            let body = #"{"matches":[{"match_id":"year-boundary","home":"甲队","away":"乙队","league":"英超","date":"12-31","date_full":"2026-12-31","time":"23:50","status":"upcoming"}]}"#
            return (200, Data(body.utf8))
        }

        let matches = try await makeClient().matches(fresh: true)
        let match = try XCTUnwrap(matches.first)
        let kickoff = try XCTUnwrap(ISO8601DateFormatter().date(from: "2026-12-31T23:50:00+08:00"))

        XCTAssertEqual(match.kickoffAt, kickoff)
        XCTAssertEqual(match.projected(at: kickoff.addingTimeInterval(45 * 60)).status, "live")
        XCTAssertEqual(match.projected(at: kickoff.addingTimeInterval(45 * 60)).minute, "45")
        let request = try XCTUnwrap(requests.all.first)
        let query = URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(query?.first(where: { $0.name == "fresh" })?.value, "1")
        XCTAssertEqual(request.value(forHTTPHeaderField: "User-Agent"), "FootballLive iOS")
    }

    func testStreamDiscoveryKeepsDistinctResolvedHLSLinesOnly() async throws {
        StubURLProtocol.install { request in
            XCTAssertEqual(request.url?.path, "/web/api/stream/9001")
            let body = #"{"sources":[{"url":"https://stream.example.test/a.m3u8","player":"m3u8","source":"line-a","name":"线路 A"},{"url":"https://stream.example.test/a-copy.m3u8","player":"m3u8","source":"line-a","name":"重复线路"},{"url":"https://stream.example.test/embed","player":"iframe","source":"line-b","name":"网页线路"},{"url":"bad url","player":"m3u8","source":"line-c","name":"损坏地址"},{"url":"https://stream.example.test/d.m3u8","player":"m3u8","source":"line-d","name":"线路 D"}]}"#
            return (200, Data(body.utf8))
        }

        let streams = try await makeClient().matchStreams(match: sampleMatch())

        XCTAssertEqual(streams.map(\.id), ["line-a", "line-d"])
        XCTAssertEqual(streams.map(\.name), ["线路 A", "线路 D"])
        XCTAssertEqual(streams.map(\.player), ["m3u8", "m3u8"])
    }

    func testSelectedStreamIsRefreshedThroughRelayWithItsReferer() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            let body = #"{"url":"https://stream.example.test/live/index.m3u8?token=a+b&x=1","source":"line-a","referer":"https://embed.example.test/watch?id=1&source=line-a"}"#
            return (200, Data(body.utf8))
        }
        let client = makeClient()
        let source = FootballStreamReference(
            id: "line-a",
            src: "line-a",
            name: "线路 A",
            player: "m3u8",
            url: URL(string: "https://stream.example.test/old.m3u8")!,
            referer: "https://embed.example.test/old"
        )

        let stream = try await client.matchStream(match: sampleMatch(), source: source)

        let request = try XCTUnwrap(requests.all.first)
        XCTAssertEqual(request.url?.path, "/web/api/stream/9001")
        let requestQuery = URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(requestQuery?.first(where: { $0.name == "src" })?.value, "line-a")
        XCTAssertEqual(requestQuery?.first(where: { $0.name == "fresh" })?.value, "1")
        let relay = try XCTUnwrap(URLComponents(url: stream.url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(relay.path, "/web/api/relay")
        XCTAssertEqual(relay.queryItems?.first(where: { $0.name == "ref" })?.value, "https://embed.example.test/watch?id=1&source=line-a")
        XCTAssertEqual(relay.queryItems?.first(where: { $0.name == "u" })?.value, "https://stream.example.test/live/index.m3u8?token=a+b&x=1")
        XCTAssertTrue(relay.percentEncodedQuery?.contains("token%3Da%2Bb%26x%3D1") == true)
    }

    func testInvalidOrChangedSelectedStreamIsRejected() async throws {
        StubURLProtocol.install { _ in
            (200, Data(#"{"url":"https://stream.example.test/replaced.m3u8","source":"line-b"}"#.utf8))
        }
        let source = FootballStreamReference(
            id: "line-a",
            src: "line-a",
            name: "线路 A",
            player: "m3u8",
            url: URL(string: "https://stream.example.test/old.m3u8")!
        )

        do {
            _ = try await makeClient().matchStream(match: sampleMatch(), source: source)
            XCTFail("A refresh must not silently switch to another source.")
        } catch let error as BackendError {
            XCTAssertEqual(error, .malformedPayload("刷新后的直播线路与所选线路不一致。"))
        }
    }

    func testRelayDoesNotWrapItsOwnURLTwiceAndPathSegmentsAreEncoded() throws {
        let client = makeClient()
        let relay = try client.endpointURL(
            segments: ["api", "relay"],
            query: [URLQueryItem(name: "u", value: "https://stream.example.test/a.m3u8")]
        )
        let encoded = try client.endpointURL(segments: ["api", "stream", "a/b ?#"])

        XCTAssertEqual(client.relayURL(for: relay.absoluteString), relay)
        XCTAssertEqual(
            URLComponents(url: encoded, resolvingAgainstBaseURL: false)?.percentEncodedPath,
            "/web/api/stream/a%2Fb%20%3F%23"
        )
    }

    func testServiceOriginMustBeAnHTTPSOrigin() {
        XCTAssertEqual(BackendClient.validatedOrigin("https://api.example.test/")?.absoluteString, "https://api.example.test")
        XCTAssertNil(BackendClient.validatedOrigin("http://api.example.test"))
        XCTAssertNil(BackendClient.validatedOrigin("https://api.example.test/path"))
        XCTAssertNil(BackendClient.validatedOrigin("https://user:password@api.example.test"))
    }

    func testPlaybackRouteCarriesTheChosenMatchAndSource() {
        let match = sampleMatch()
        let source = FootballStreamReference(
            id: "line-a",
            src: "line-a",
            name: "线路 A",
            player: "m3u8",
            url: URL(string: "https://stream.example.test/a.m3u8")!
        )

        let route = PlaybackRoute(footballMatch: match, stream: source)

        XCTAssertEqual(route.match.id, match.id)
        XCTAssertEqual(route.stream.id, source.id)
        XCTAssertEqual(route.title, "甲队 vs 乙队")
    }
}

private func makeClient() -> BackendClient {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [StubURLProtocol.self]
    return BackendClient(
        origin: URL(string: "https://api.example.test")!,
        session: URLSession(configuration: configuration)
    )
}

private func sampleMatch() -> FootballMatch {
    FootballMatch(
        id: "9001",
        title: "甲队 vs 乙队",
        home: "甲队",
        away: "乙队",
        league: "英超",
        time: "20:00",
        status: "live"
    )
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
        guard let handler, let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        let (statusCode, payload) = handler(request)
        guard let response = HTTPURLResponse(
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
