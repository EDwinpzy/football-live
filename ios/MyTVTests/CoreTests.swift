import CryptoKit
import Foundation
import XCTest
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

    func testFootballUsesChannelIDAndReturnsAlreadyRelayedURL() async throws {
        let requests = RequestRecorder()
        StubURLProtocol.install { request in
            requests.append(request)
            if request.url?.path == "/web/api/matches" {
                let body = #"{"count":1,"matches":[{"match_id":"9001","title":"甲队 vs 乙队","home":"甲队","away":"乙队","league":"联赛","date":"2026-10-09","time":"20:00","status":"live","minute":"32","score":{"home":1,"away":0},"channels":[{"id":"8123","src":"plu","name":"线路甲"}]}]}"#
                return (200, Data(body.utf8))
            }
            let body = #"{"url":"https://stream.example.test/live/index.m3u8?token=a+b&x=1"}"#
            return (200, Data(body.utf8))
        }
        let client = makeClient()
        let matches = try await client.matches()
        let match = try XCTUnwrap(matches.first)

        XCTAssertEqual(match.id, "9001")
        XCTAssertEqual(match.score, "1-0")
        XCTAssertEqual(match.streams.first?.id, "8123")
        let stream = try await client.matchStream(match: match, sourceIndex: 0)

        let streamRequest = try XCTUnwrap(requests.all.first { $0.url?.path.contains("/api/stream/") == true })
        XCTAssertEqual(streamRequest.url?.path, "/web/api/stream/8123")
        XCTAssertEqual(URLComponents(url: try XCTUnwrap(streamRequest.url), resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "src" })?.value, "plu")
        XCTAssertEqual(stream.url.path, "/web/api/relay")
        let relayComponents = try XCTUnwrap(URLComponents(url: stream.url, resolvingAgainstBaseURL: false))
        XCTAssertNil(relayComponents.queryItems?.first(where: { $0.name == "ref" }))
        XCTAssertEqual(relayComponents.queryItems?.first(where: { $0.name == "u" })?.value, "https://stream.example.test/live/index.m3u8?token=a+b&x=1")
        XCTAssertTrue(relayComponents.percentEncodedQuery?.contains("a%2Bb%26x%3D1") == true)
    }

    func testRelayDoesNotWrapItsOwnURLTwice() throws {
        let client = makeClient()
        let existing = try client.endpointURL(segments: ["api", "relay"], query: [URLQueryItem(name: "u", value: "https://stream.example.test/a.m3u8")])

        XCTAssertEqual(client.relayURL(for: existing.absoluteString), existing)
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
