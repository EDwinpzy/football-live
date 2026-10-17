import Foundation

struct PlaybackRetryTarget: Equatable {
    let source: MediaSource
    let episode: Episode
    let resumeTime: TimeInterval
}

enum PlaybackFailureAction: Equatable {
    case relay(URL)
    case retry(PlaybackRetryTarget)
    case showFailure
}

enum PlaybackRetryPolicy {
    static let retryDelayNanoseconds: UInt64 = 800_000_000

    static func target(
        source: MediaSource?,
        episode: Episode?,
        resumeTime: TimeInterval
    ) -> PlaybackRetryTarget? {
        guard let source, let episode else { return nil }
        return PlaybackRetryTarget(
            source: source,
            episode: episode,
            resumeTime: safeTime(resumeTime, fallback: 0)
        )
    }

    static func failureAction(
        isVOD: Bool,
        isDirectAttempt: Bool,
        relayURL: URL?,
        source: MediaSource?,
        episode: Episode?,
        resumeTime: TimeInterval
    ) -> PlaybackFailureAction {
        if isVOD, isDirectAttempt, let relayURL {
            return .relay(relayURL)
        }
        if isVOD,
           let target = target(source: source, episode: episode, resumeTime: resumeTime) {
            return .retry(target)
        }
        return .showFailure
    }

    static func safeTime(_ time: TimeInterval, fallback: TimeInterval) -> TimeInterval {
        if time.isFinite, time >= 0 { return time }
        return fallback.isFinite ? max(0, fallback) : 0
    }

    static func resumeTime(
        playerTime: TimeInterval?,
        pendingSeekTarget: TimeInterval?,
        savedPlaybackTime: TimeInterval
    ) -> TimeInterval {
        if let pendingSeekTarget {
            return safeTime(pendingSeekTarget, fallback: savedPlaybackTime)
        }
        return safeTime(playerTime ?? .nan, fallback: savedPlaybackTime)
    }
}
