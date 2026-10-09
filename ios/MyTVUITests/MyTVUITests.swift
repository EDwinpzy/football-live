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
        XCTAssertTrue(
            firstCard.waitForExistence(timeout: 45),
            "The real service should load at least one movie card without activation.\n\(app.debugDescription)"
        )
        assertFrame(of: app, landscape: false)

        let posterLoaded = NSPredicate(format: "value == %@", "海报已加载")
        let posterExpectation = XCTNSPredicateExpectation(predicate: posterLoaded, object: firstCard)
        XCTAssertEqual(
            XCTWaiter.wait(for: [posterExpectation], timeout: 30),
            .completed,
            "The first movie poster should finish loading before capture.\n\(app.debugDescription)"
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
        XCTAssertEqual(app.buttons["category-tv"].label, "电视剧")
        XCTAssertEqual(app.buttons["category-anime"].label, "3D国漫")
        attachScreenshot(of: app, named: "fixture-home-portrait")

        firstCard.tap()
        let summary = app.staticTexts["detail-summary"]
        XCTAssertTrue(summary.waitForExistence(timeout: 10), "Fixture detail content should be visible before capture.")
        XCTAssertTrue(app.buttons["episode-fixture-source-fixture-episode-1"].exists)
        attachScreenshot(of: app, named: "fixture-detail-portrait")
    }

    func testFixtureHomeShowsTwoRowsAndCanTurnToTheNextPage() {
        let app = fixtureApp()
        app.launch()

        let firstCard = app.buttons["media-card-fixture-movie-1"]
        let nextRowCard = app.buttons["media-card-fixture-movie-4"]
        XCTAssertTrue(firstCard.waitForExistence(timeout: 10))
        XCTAssertTrue(nextRowCard.exists)
        XCTAssertGreaterThan(nextRowCard.frame.minY, firstCard.frame.minY)
        XCTAssertTrue(nextRowCard.isHittable)

        let secondPage = app.buttons["home-page-fixture-featured-2"]
        XCTAssertTrue(secondPage.waitForExistence(timeout: 10))
        secondPage.tap()
        let laterCard = app.buttons["media-card-fixture-movie-7"]
        XCTAssertTrue(laterCard.waitForExistence(timeout: 10))
        XCTAssertTrue(laterCard.isHittable)
        attachScreenshot(of: app, named: "fixture-home-second-page-portrait")
    }

    func testFixtureDetailCapturesLandscape() {
        let app = fixtureApp()
        app.launch()

        let firstCard = app.buttons["media-card-fixture-movie-1"]
        XCTAssertTrue(firstCard.waitForExistence(timeout: 10))
        firstCard.tap()
        let summary = app.staticTexts["detail-summary"]
        XCTAssertTrue(summary.waitForExistence(timeout: 10))

        XCUIDevice.shared.orientation = .landscapeLeft
        let title = app.staticTexts["detail-title"]
        waitForStableLandscapeDetailLayout(app: app, title: title, summary: summary)
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

        let licenseGate = app.staticTexts["播放需要激活"]
        XCTAssertTrue(licenseGate.waitForExistence(timeout: 10), "An unlicensed fixture playback route must stop at the activation gate.")
        XCTAssertTrue(licenseGate.isHittable, "The activation gate should be visible to the user.")
        XCTAssertFalse(app.descendants(matching: .any)["native-video-player"].exists, "The activation gate must appear before AVPlayer is created.")
        attachScreenshot(of: app, named: "fixture-player-license-gate")
    }

    private func fixtureApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments.append("--ui-test-fixtures")
        return app
    }

    private func attachScreenshot(of app: XCUIApplication, named name: String) {
        let screenshot = name.contains("landscape") ? XCUIScreen.main.screenshot() : app.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
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

    private func waitForStableLandscapeDetailLayout(
        app: XCUIApplication,
        title: XCUIElement,
        summary: XCUIElement,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let navigationBar = app.navigationBars.firstMatch
        let backButton = navigationBar.buttons.element(boundBy: 0)
        let layoutReady = NSPredicate { _, _ in
            self.landscapeDetailGeometry(
                app: app,
                title: title,
                summary: summary,
                navigationBar: navigationBar,
                backButton: backButton
            ) != nil
        }
        let layoutExpectation = XCTNSPredicateExpectation(predicate: layoutReady, object: app)
        XCTAssertEqual(
            XCTWaiter.wait(for: [layoutExpectation], timeout: 15),
            .completed,
            "Landscape detail title, summary, and back button should be visible before capture.\n\(app.debugDescription)",
            file: file,
            line: line
        )

        var lastGeometry: [CGRect]?
        var stableSamples = 0
        let stabilityDeadline = Date().addingTimeInterval(5)
        while Date() < stabilityDeadline && stableSamples < 3 {
            if let geometry = landscapeDetailGeometry(
                app: app,
                title: title,
                summary: summary,
                navigationBar: navigationBar,
                backButton: backButton
            ) {
                if let lastGeometry, rectsAreStable(lastGeometry, geometry) {
                    stableSamples += 1
                } else {
                    stableSamples = 1
                }
                lastGeometry = geometry
            } else {
                stableSamples = 0
                lastGeometry = nil
            }

            if stableSamples < 3 {
                RunLoop.current.run(until: Date().addingTimeInterval(0.3))
            }
        }

        XCTAssertGreaterThanOrEqual(
            stableSamples,
            3,
            "Landscape detail geometry should remain stable across repeated samples.\n\(app.debugDescription)",
            file: file,
            line: line
        )

        // XCTest can observe the new frame before UIKit finishes the rotation animation.
        RunLoop.current.run(until: Date().addingTimeInterval(1.0))
        let settledGeometry = landscapeDetailGeometry(
            app: app,
            title: title,
            summary: summary,
            navigationBar: navigationBar,
            backButton: backButton
        )
        XCTAssertNotNil(
            settledGeometry,
            "Landscape detail content should remain in bounds after the rotation settles.\n\(app.debugDescription)",
            file: file,
            line: line
        )
        if let lastGeometry, let settledGeometry {
            XCTAssertTrue(
                rectsAreStable(lastGeometry, settledGeometry),
                "Landscape detail geometry should stay stable through the final screenshot settle.\n\(app.debugDescription)",
                file: file,
                line: line
            )
        }
    }

    private func landscapeDetailGeometry(
        app: XCUIApplication,
        title: XCUIElement,
        summary: XCUIElement,
        navigationBar: XCUIElement,
        backButton: XCUIElement
    ) -> [CGRect]? {
        let screen = app.frame
        guard screen.width > screen.height,
              title.exists,
              summary.exists,
              navigationBar.exists,
              backButton.exists,
              title.isHittable,
              summary.isHittable,
              backButton.isHittable else {
            return nil
        }

        let titleFrame = title.frame
        let summaryFrame = summary.frame
        let navigationBarFrame = navigationBar.frame
        let backButtonFrame = backButton.frame
        guard screen.contains(titleFrame),
              titleFrame.height < screen.height,
              screen.intersects(summaryFrame),
              screen.contains(navigationBarFrame),
              screen.contains(backButtonFrame) else {
            return nil
        }

        return [screen, titleFrame, summaryFrame, navigationBarFrame, backButtonFrame]
    }

    private func rectsAreStable(_ lhs: [CGRect], _ rhs: [CGRect], tolerance: CGFloat = 1) -> Bool {
        guard lhs.count == rhs.count else { return false }
        return zip(lhs, rhs).allSatisfy { pair in
            let (first, second) = pair
            return abs(first.minX - second.minX) <= tolerance &&
                abs(first.minY - second.minY) <= tolerance &&
                abs(first.width - second.width) <= tolerance &&
                abs(first.height - second.height) <= tolerance
        }
    }
}
