import SwiftUI
import AVFoundation
import AppKit

/// Video surface backed by `AVPlayerLayer` (pure AVFoundation), wrapped in a
/// plain `NSView`. We deliberately avoid AVKit's SwiftUI `VideoPlayer` /
/// `AVPlayerView`: in a bare Swift Package executable (no app bundle) the
/// `_AVKit_SwiftUI` `AVPlayerView` class metadata fails to demangle and crashes
/// at view-make time. A standard NSView hosting a CALayer has no such issue.
struct PlayerLayerView: NSViewRepresentable {
    let player: AVPlayer?

    func makeNSView(context: Context) -> NSView {
        let view = PlayerContainerView()
        view.playerLayer.player = player
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        (nsView as? PlayerContainerView)?.playerLayer.player = player
    }
}

final class PlayerContainerView: NSView {
    let playerLayer = AVPlayerLayer()

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        let host = CALayer()
        layer = host
        playerLayer.videoGravity = .resizeAspect
        playerLayer.backgroundColor = NSColor.clear.cgColor
        host.addSublayer(playerLayer)
    }

    required init?(coder: NSCoder) { nil }

    override func layout() {
        super.layout()
        playerLayer.frame = bounds
    }
}
