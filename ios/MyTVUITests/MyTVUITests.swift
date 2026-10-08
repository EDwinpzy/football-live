import XCTest

// Fixture captures validate layout and state rendering only; the real-service test is separate.
final class MyTVUITests: XCTestCase {
    override func setUp() {
        continueAfterFailure = false
        XCUIDevice.shared.orientation = .portrait
    }

    func testRealServiceHomeLoadsWithoutActivationAndCapturesPortrait() {
        let app = XCUIApplication()
        app.launch()

        let firstCard = app.buttons.matching(
            NSPredicate(format: "identifier BEGINSWITH %@", "media-card-")
        ).firstMatch
        XCTAssertTrue(firstCard.waitForExistence(timeout: 45), "The real service should load at least one movie card without activation.")
        assertFrame(of: app, landscape: false)

        let posterLoaded = NSPredicate(format: "value == %@", "海报已加载")
        let posterExpectation = XCTNSPredicateExpectation(predicate: posterLoaded, object: firstCard)
        XCTAssertEqual(
            XCTWaiter.wait(for: [posterExpectation], timeout: 30),
            .completed,
            "The first movie poster should finish loading before capture."
        )
        attachScreenshot(of: app, named: "real-service-home-portrait")
    }

    func testFixtureHomeAndDetailCapturePortrait() {
        let app = fixtureApp()
        app.launch()

        let firstCard = app.buttons["media-card-fixture-movie-1"]
        XCTAssertTrue(firstCard.waitForExistence(timeout: 10))
        assertFrame(of: app, landscape: false)
        XCTAssertTrue(app.staticTexts["home-title"].exists)
        attachScreenshot(of: app, named: "fixture-home-portrait")

        firstCard.tap()
        let summary = app.staticTexts["detail-summary"]
        XCTAssertTrue(summary.waitForExistence(timeout: 10), "Fixture detail content should be visible before capture.")
        XCTAssertTrue(app.buttons["episode-fixture-source-fixture-episode-1"].exists)
        attachScreenshot(of: app, named: "fixture-detail-portrait")
    }

    func testFixtureDetailCapturesLandscape() {
        let app = fixtureApp()
        app.launch()

        let firstCard = app.buttons["media-card-fixture-movie-1"]
        XCTAssertTrue(firstCard.waitForExistence(timeout: 10))
        firstCard.tap()
        XCTAssertTrue(app.staticTexts["detail-summary"].waitForExistence(timeout: 10))

        XCUIDevice.shared.orientation = .landscapeLeft
        let title = app.staticTexts["detail-title"]
        XCTAssertTrue(title.waitForExistence(timeout: 8))
        assertFrame(of: app, landscape: true)
        attachScreenshot(of: app, named: "fixture-detail-landscape")
        XCUIDevice.shared.orientation = .portrait
    }

    func testFixtureUnlicensedEpisodeShowsPlayerGateWithoutStreamRequest() {
        let app = fixtureApp()
        app.launch()

        let firstCard = app.buttons["media-card-fixture-movie-1"]
        XCTAssertTrue(firstCard.waitForExistence(timeout: 10))
        firstCard.tap()

        let summary = app.staticTexts["detail-summary"]
        XCTAssertTrue(summary.waitForExistence(timeout: 10))
        let episode = app.buttons["episode-fixture-source-fixture-episode-1"]
        XCTAssertTrue(episode.waitForExistence(timeout: 10))
        episode.tap()

        let licenseGate = app.descendants(matching: .any)["player-license-gate"]
        XCTAssertTrue(licenseGate.waitForExistence(timeout: 10), "An unlicensed fixture playback route must stop at the activation gate.")
        XCTAssertFalse(app.descendants(matching: .any)["native-video-player"].exists, "The activation gate must appear before AVPlayer is created.")
        attachScreenshot(of: app, named: "fixture-player-license-gate")
    }

    private func fixtureApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments.append("--ui-test-fixtures")
        return app
    }

    private func attachScreenshot(of app: XCUIApplication, named name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func assertFrame(of app: XCUIApplication, landscape: Bool, file: StaticString = #filePath, line: UInt = #line) {
        let orientationPredicate = NSPredicate { _, _ in
            landscape ? app.frame.width > app.frame.height : app.frame.height > app.frame.width
        }
        let orientationExpectation = XCTNSPredicateExpectation(predicate: orientationPredicate, object: app)
        XCTAssertEqual(XCTWaiter.wait(for: [orientationExpectation], timeout: 8), .completed, file: file, line: line)
    }
}
