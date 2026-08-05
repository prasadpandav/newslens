import SwiftUI
import UIKit
import ImageIO

/// Publisher artwork, decoded at the size it will actually be drawn.
///
/// This replaces `AsyncImage`, which was the single biggest cause of the app
/// feeling unresponsive. `AsyncImage` hands the downloaded bytes to `Image`,
/// which decodes them **on the main thread at full resolution**. The feed's
/// artwork comes straight from publisher CDNs at around 1024px wide, so every
/// 84×84 thumbnail was costing a ~2.4MB main-thread decode, and a feed of thirty
/// stories paid that thirty times while the reader was trying to tap something.
/// It also has no cache of its own beyond URLSession's, so scrolling away and
/// back paid again.
///
/// Here the bytes are downsampled by ImageIO on a background task — ImageIO
/// reads only the pixels it needs rather than decoding the whole image and
/// throwing most of it away — and the result is kept in a small memory cache.
/// The main thread only ever composites a bitmap that is already the right size.
@MainActor
final class ThumbnailStore {
    static let shared = ThumbnailStore()

    /// Cost-limited rather than count-limited: 40 full-width hero images and
    /// 40 thumbnails are wildly different amounts of memory, and this app runs
    /// beside a backend on a small box — being casual about image memory is how
    /// you get jetsammed.
    private let cache: NSCache<NSString, UIImage> = {
        let c = NSCache<NSString, UIImage>()
        c.totalCostLimit = 32 * 1024 * 1024
        return c
    }()
    /// In-flight loads, so ten rows asking for the same publisher image while
    /// scrolling issue one request instead of ten.
    private var inFlight: [String: Task<UIImage?, Never>] = [:]

    private init() {
        // Free the cache under pressure instead of being killed for holding it.
        NotificationCenter.default.addObserver(
            forName: UIApplication.didReceiveMemoryWarningNotification,
            object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.cache.removeAllObjects() }
            }
    }

    private func key(_ url: URL, _ maxPixel: CGFloat) -> NSString {
        "\(url.absoluteString)|\(Int(maxPixel))" as NSString
    }

    func cached(_ url: URL, maxPixel: CGFloat) -> UIImage? {
        cache.object(forKey: key(url, maxPixel))
    }

    func load(_ url: URL, maxPixel: CGFloat) async -> UIImage? {
        let k = key(url, maxPixel)
        if let hit = cache.object(forKey: k) { return hit }
        if let running = inFlight[k as String] { return await running.value }

        let task = Task<UIImage?, Never> { [maxPixel] in
            guard let (data, response) = try? await URLSession.shared.data(from: url),
                  (response as? HTTPURLResponse).map({ (200..<300).contains($0.statusCode) }) ?? true
            else { return nil }
            // Detached: the decode must not run on the main actor, which is what
            // this whole type exists to avoid.
            return await Task.detached(priority: .utility) {
                ThumbnailStore.downsample(data, maxPixel: maxPixel)
            }.value
        }
        inFlight[k as String] = task
        let image = await task.value
        inFlight[k as String] = nil
        if let image {
            cache.setObject(image, forKey: k, cost: image.byteCost)
        }
        return image
    }

    /// ImageIO reads the file's own scaled representation where it has one and
    /// otherwise decodes directly to the requested size — it never materialises
    /// the full-resolution bitmap the way `UIImage(data:)` does.
    nonisolated static func downsample(_ data: Data, maxPixel: CGFloat) -> UIImage? {
        let srcOptions = [kCGImageSourceShouldCache: false] as CFDictionary
        guard let src = CGImageSourceCreateWithData(data as CFData, srcOptions) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            // Decode NOW, on this background task. Without it the decode is
            // deferred to first draw — which happens on the main thread, which
            // is exactly the stall we are removing.
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixel,
        ]
        guard let cg = CGImageSourceCreateThumbnailAtIndex(src, 0, options as CFDictionary)
        else { return nil }
        return UIImage(cgImage: cg)
    }
}

private extension UIImage {
    var byteCost: Int {
        guard let cg = cgImage else { return 1 }
        return cg.bytesPerRow * cg.height
    }
}

/// Draws publisher artwork at a fixed size, or nothing at all.
///
/// Collapses to zero height when there is no image OR when loading fails: these
/// URLs are hotlinked from publisher CDNs, some of which 404, block hotlinking,
/// or are unreachable on a given network, and an empty grey box in the middle of
/// a card reads as a bug. Text-only is the honest fallback.
struct StoryImage: View {
    @Environment(\.palette) private var pal
    @Environment(\.displayScale) private var scale

    let urlString: String?
    var height: CGFloat = 168
    /// Fixed width for the list thumbnail; nil means "as wide as offered".
    var width: CGFloat? = nil
    /// The lead story's photograph runs edge to edge under the card's top
    /// corners rather than floating inside the padding, so it is clipped by the
    /// card and takes no rounding of its own.
    var squareTop: Bool = false

    @State private var image: UIImage?
    @State private var failed = false

    private var url: URL? {
        guard let s = urlString, !s.isEmpty else { return nil }
        return URL(string: s)
    }

    /// The longest edge this will ever be drawn at, in real pixels. A hero is
    /// full-bleed so it is asked for at a generous full-width figure; a
    /// thumbnail asks for 84. Deliberately quantised: keying the cache on the
    /// exact measured width would miss on every rotation and every device, and
    /// the point of the cache is that scrolling back does not re-decode.
    private var maxPixel: CGFloat {
        let points = max(width ?? 440, height)
        return points * max(scale, 1)
    }

    var body: some View {
        if let url, !failed {
            content(url)
                .frame(maxWidth: width ?? .infinity)
                .frame(width: width, height: height)
                .clipped()
                .clipShape(RoundedRectangle(cornerRadius: squareTop ? 0 : pal.r(7),
                                            style: .continuous))
                .accessibilityHidden(true)   // decorative; the headline carries meaning
                // `id:` — a recycled row in a LazyVStack keeps its @State, so
                // without this a scrolled-away row would keep showing the
                // previous story's photograph.
                .task(id: url) { await load(url) }
        }
    }

    @ViewBuilder
    private func content(_ url: URL) -> some View {
        if let image {
            Image(uiImage: image).resizable().aspectRatio(contentMode: .fill)
        } else {
            // A tint rather than a spinner: a spinner per row on a scrolling
            // feed is visual noise, and these resolve in a frame or two once
            // the image is in the cache.
            Rectangle().fill(pal.surface2)
        }
    }

    private func load(_ url: URL) async {
        // Synchronous cache hit avoids a frame of empty box on an image the
        // reader has already seen this session.
        if let hit = ThumbnailStore.shared.cached(url, maxPixel: maxPixel) {
            image = hit
            return
        }
        image = nil
        let loaded = await ThumbnailStore.shared.load(url, maxPixel: maxPixel)
        if let loaded {
            image = loaded
        } else {
            failed = true
        }
    }
}
