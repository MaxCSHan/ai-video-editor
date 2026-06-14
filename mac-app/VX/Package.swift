// swift-tools-version:5.9
import PackageDescription

// VX — native macOS app. Runnable via `swift run VX` for fast iteration;
// see README.md for wrapping in an Xcode project for distribution.
let package = Package(
    name: "VX",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "VX",
            path: "Sources/VX"
        )
    ]
)
