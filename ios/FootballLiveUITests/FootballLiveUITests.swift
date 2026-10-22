import XCTest

final class FootballLiveUITests: XCTestCase {
    override func setUp() {
        continueAfterFailure = false
        XCUIDevice.shared.orientation = .portrait
    }

    func testFootballLiveIsTheOnlyHomeAndServiceSettingsRemainAvailable() {
        let app = XCUIApplication()
        app.launch()

        XCTAssertTrue(app.navigationBars["足球直播"].waitForExistence(timeout: 15))
        XCTAssertEqual(app.tabBars.count, 0, "The product should open directly into the football live screen.")
        XCTAssertFalse(app.buttons["tab-home"].exists)
        XCTAssertFalse(app.buttons["tab-search"].exists)
        XCTAssertFalse(app.buttons["tab-live"].exists)
        XCTAssertFalse(app.buttons["tab-library"].exists)
        attachScreenshot(of: app, named: "football-live-home")

        let settingsLink = app.buttons["football-settings-link"]
        XCTAssertTrue(settingsLink.waitForExistence(timeout: 5))
        settingsLink.tap()

        XCTAssertTrue(app.textFields["service-origin-field"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["自用版激活"].exists)
        XCTAssertFalse(app.staticTexts["收藏"].exists)
        XCTAssertFalse(app.staticTexts["搜索影视内容"].exists)
        attachScreenshot(of: app, named: "football-live-service-settings")
    }

    private func attachScreenshot(of app: XCUIApplication, named name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
