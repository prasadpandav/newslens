import SwiftUI

// MARK: - Adaptive color helper (follows the device's light/dark mode)

extension Color {
    init(light: Color, dark: Color) {
        self.init(uiColor: UIColor { traits in
            traits.userInterfaceStyle == .dark ? UIColor(dark) : UIColor(light)
        })
    }
}

// MARK: - Bluelligent Native design tokens (adaptive)

enum BL {
    // Backgrounds: deep ink in dark mode, soft paper in light mode.
    static let ink = Color(light: Color(red: 0.965, green: 0.973, blue: 0.988),
                           dark:  Color(red: 0.027, green: 0.043, blue: 0.078))
    static let ink2 = Color(light: .white,
                            dark:  Color(red: 0.043, green: 0.067, blue: 0.125))
    static let surface = Color(light: .white,
                               dark:  Color.white.opacity(0.045))
    static let surface2 = Color(light: Color.black.opacity(0.05),
                                dark:  Color.white.opacity(0.07))
    static let hairline = Color(light: Color.black.opacity(0.08),
                                dark:  Color.white.opacity(0.08))
    static let hairline2 = Color(light: Color.black.opacity(0.15),
                                 dark:  Color.white.opacity(0.14))
    static let text2 = Color(light: Color(red: 0.35, green: 0.39, blue: 0.47),
                             dark:  Color(red: 0.604, green: 0.647, blue: 0.722))

    // Semantic colors: darker variants in light mode for WCAG contrast on paper.
    static let accent = Color(light: Color(red: 0.13, green: 0.42, blue: 0.90),
                              dark:  Color(red: 0.302, green: 0.624, blue: 1.0))
    static let ai = Color(light: Color(red: 0.40, green: 0.27, blue: 0.92),
                          dark:  Color(red: 0.486, green: 0.361, blue: 1.0))
    static let trust = Color(light: Color(red: 0.02, green: 0.59, blue: 0.41),
                             dark:  Color(red: 0.239, green: 0.863, blue: 0.592))
    static let warning = Color(light: Color(red: 0.72, green: 0.46, blue: 0.02),
                               dark:  Color(red: 1.0, green: 0.761, blue: 0.302))
    static let breaking = Color(light: Color(red: 0.83, green: 0.15, blue: 0.24),
                                dark:  Color(red: 1.0, green: 0.365, blue: 0.451))
    static let prediction = Color(light: Color(red: 0.49, green: 0.23, blue: 0.93),
                                  dark:  Color(red: 0.706, green: 0.549, blue: 1.0))

    static let aiGradient = LinearGradient(colors: [accent, ai],
                                           startPoint: .topLeading, endPoint: .bottomTrailing)
    static let spring = Animation.spring(response: 0.45, dampingFraction: 0.85)

    static func credColor(_ score: Double) -> Color {
        score >= 75 ? trust : score >= 50 ? warning : breaking
    }
}

// MARK: - Background (ambient radial glows; adapts to mode)

struct InkBackground: View {
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        ZStack {
            BL.ink.ignoresSafeArea()
            RadialGradient(colors: [BL.accent.opacity(scheme == .dark ? 0.13 : 0.07), .clear],
                           center: .init(x: 0.85, y: -0.05), startRadius: 0, endRadius: 420)
            RadialGradient(colors: [BL.ai.opacity(scheme == .dark ? 0.10 : 0.05), .clear],
                           center: .init(x: 0.05, y: 1.05), startRadius: 0, endRadius: 380)
        }
        .ignoresSafeArea()
    }
}

// MARK: - Liquid Glass with graceful fallback (functional layer only)

extension View {
    /// iOS 26 Liquid Glass on the functional layer; material fallback earlier.
    @ViewBuilder
    func blGlass(in shape: some Shape = Capsule()) -> some View {
        if #available(iOS 26.0, *) {
            self.glassEffect(.regular, in: shape)
        } else {
            self.background(.ultraThinMaterial, in: shape)
                .overlay(shape.stroke(BL.hairline2, lineWidth: 1))
        }
    }

    /// Content-layer card: opaque surface (never glass, per HIG guidance).
    /// Dark mode: ink surface + hairline. Light mode: white card + soft shadow.
    func blCard(radius: CGFloat = 18) -> some View {
        self.background(
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .fill(BL.surface)
                .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .stroke(BL.hairline, lineWidth: 1)))
            .shadow(color: Color(light: .black.opacity(0.06), dark: .clear),
                    radius: 10, y: 4)
    }
}

// MARK: - Chip

struct Chip: View {
    var text: String
    var color: Color = BL.text2
    var filled: Bool = false

    var body: some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(Capsule().fill(filled ? color.opacity(0.16) : BL.surface2))
            .overlay(Capsule().stroke(filled ? color.opacity(0.4) : BL.hairline, lineWidth: 1))
            .foregroundStyle(filled ? color : BL.text2)
    }
}

// MARK: - Wrapping chip row

/// A left-aligned row that wraps onto new lines instead of squeezing its
/// children. Chip rows have grown past one line at larger Dynamic Type sizes,
/// and HStack would rather compress every chip than wrap.
struct BLFlow: Layout {
    var spacing: CGFloat = 8
    var lineSpacing: CGFloat = 8

    /// Natural size, but never wider than the row. A chip whose label is longer
    /// than the whole row (some "who's affected" entries are full phrases) would
    /// otherwise be measured and placed at its unconstrained width and spill out
    /// of its own capsule; clamping makes it wrap and grow taller instead.
    private func fit(_ v: LayoutSubview, _ maxWidth: CGFloat) -> CGSize {
        let natural = v.sizeThatFits(.unspecified)
        guard natural.width > maxWidth, maxWidth.isFinite else { return natural }
        return v.sizeThatFits(ProposedViewSize(width: maxWidth, height: nil))
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, lineHeight: CGFloat = 0, widest: CGFloat = 0
        for v in subviews {
            let s = fit(v, maxWidth)
            if x > 0, x + spacing + s.width > maxWidth {   // doesn't fit — new line
                y += lineHeight + lineSpacing
                x = 0; lineHeight = 0
            }
            x += (x > 0 ? spacing : 0) + s.width
            lineHeight = max(lineHeight, s.height)
            widest = max(widest, x)
        }
        return CGSize(width: min(widest, maxWidth), height: y + lineHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, lineHeight: CGFloat = 0
        for v in subviews {
            let s = fit(v, bounds.width)
            if x > bounds.minX, x + spacing + s.width > bounds.maxX {
                y += lineHeight + lineSpacing
                x = bounds.minX; lineHeight = 0
            }
            if x > bounds.minX { x += spacing }
            // Placed at the measured size, not `.unspecified`, so a clamped chip
            // is actually laid out at the width it was measured for.
            v.place(at: CGPoint(x: x, y: y), anchor: .topLeading,
                    proposal: ProposedViewSize(width: s.width, height: s.height))
            x += s.width
            lineHeight = max(lineHeight, s.height)
        }
    }
}

// MARK: - Category labels

extension String {
    /// Category display label: sentence case, except "ai" which stays fully
    /// capital ("AI", never "Ai"). `.capitalized` alone gets this wrong for AI —
    /// use this instead of `.capitalized` for topic/category strings
    /// (world, business, finance, ai, ...). Leaves the raw value untouched
    /// everywhere it's used for filtering/comparison — display only.
    var topicLabel: String {
        self.lowercased() == "ai" ? "AI"
            : self.prefix(1).uppercased() + self.dropFirst().lowercased()
    }
}

// MARK: - "Last told" timestamps

/// When Descry last told this story/trend/forecast. The backend bumps the same
/// `created_at` when a developing story is retold with new facts, so one stamp
/// covers both a first telling and its latest retelling.
enum LastTold {
    /// Short relative form: "just now", "12m ago", "3h ago", "2d ago", then a
    /// date once it's older than a week (relative loses meaning past that).
    static func relative(_ epochSeconds: Double?) -> String? {
        guard let epochSeconds, epochSeconds > 0 else { return nil }
        let elapsed = Date().timeIntervalSince(Date(timeIntervalSince1970: epochSeconds))
        guard elapsed >= 0 else { return "just now" }   // clock skew
        switch elapsed {
        case ..<60:     return "just now"
        case ..<3600:   return "\(Int(elapsed / 60))m ago"
        case ..<86_400: return "\(Int(elapsed / 3600))h ago"
        case ..<604_800: return "\(Int(elapsed / 86_400))d ago"
        default:
            return Date(timeIntervalSince1970: epochSeconds)
                .formatted(.dateTime.month(.abbreviated).day())
        }
    }

    /// Full timestamp for accessibility labels and detail lines.
    static func full(_ epochSeconds: Double?) -> String? {
        guard let epochSeconds, epochSeconds > 0 else { return nil }
        return Date(timeIntervalSince1970: epochSeconds)
            .formatted(date: .abbreviated, time: .shortened)
    }
}

/// The "Last told 3h ago" chip. Renders nothing when there's no timestamp, so
/// callers don't each need their own `if let`.
struct LastToldChip: View {
    var at: Double?
    var prefix: String = "Last told"

    var body: some View {
        if let rel = LastTold.relative(at) {
            Chip(text: "\(prefix) \(rel)")
                .accessibilityLabel(Text("\(prefix) \(LastTold.full(at) ?? rel)"))
        }
    }
}

/// Compact caption form for dense rows (cards) where a full chip is too heavy.
struct LastToldLabel: View {
    var at: Double?

    var body: some View {
        if let rel = LastTold.relative(at) {
            Text(rel)
                .font(.caption2)
                .foregroundStyle(BL.text2)
                .accessibilityLabel(Text("Last told \(LastTold.full(at) ?? rel)"))
        }
    }
}

// MARK: - Trust / corroboration ring

struct TrustRing: View {
    var score: Double
    var size: CGFloat = 46
    @State private var animated = false

    var body: some View {
        ZStack {
            Circle().stroke(Color.white.opacity(0.09), lineWidth: 4.5)
            Circle()
                .trim(from: 0, to: animated ? score / 100 : 0)
                .stroke(BL.credColor(score),
                        style: StrokeStyle(lineWidth: 4.5, lineCap: .round))
                .rotationEffect(.degrees(-90))
            Text("\(Int(score))")
                .font(.system(size: size * 0.3, weight: .bold, design: .monospaced))
                .foregroundStyle(BL.credColor(score))
        }
        .frame(width: size, height: size)
        .onAppear { withAnimation(BL.spring.delay(0.2)) { animated = true } }
        .accessibilityLabel("Corroboration \(Int(score)) percent")
    }
}

// MARK: - Trust meter bar

struct TrustMeter: View {
    var score: Double

    /// Mirrors BL.credColor's own breakpoints, so the word and the bar's color
    /// never disagree. Replaces the card's old standalone "Highly corroborated"
    /// chip — that only ever appeared above 85 and left every other card's bar
    /// unlabeled; this labels all of them and lives right on the number it explains.
    private var label: String {
        score >= 75 ? "Highly corroborated" : score >= 50 ? "Developing story" : "Limited corroboration"
    }

    var body: some View {
        HStack(spacing: 8) {
            Text(label)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(BL.credColor(score))
                .lineLimit(1)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.white.opacity(0.08))
                    Capsule().fill(BL.credColor(score))
                        .frame(width: geo.size.width * score / 100)
                }
            }
            .frame(height: 5)
            Text("\(Int(score))%")
                .font(.caption2.weight(.semibold).monospaced())
                .foregroundStyle(BL.credColor(score))
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Impact badge

struct ImpactBadge: View {
    var score: Int
    private var label: String { ["", "For you", "Affects you", "High impact"][min(score, 3)] }
    private var color: Color { [BL.text2, BL.accent, BL.warning, BL.breaking][min(score, 3)] }
    var body: some View {
        if score > 0 { Chip(text: label, color: color, filled: true) }
    }
}

// MARK: - Sparkline (deterministic from a seed string, drawn with Canvas)

struct Sparkline: View {
    var seed: String
    var color: Color = BL.accent
    var width: CGFloat = 72
    var height: CGFloat = 22

    var body: some View {
        let pts = Self.points(seed: seed)
        Canvas { ctx, size in
            var path = Path()
            for (i, p) in pts.enumerated() {
                let pt = CGPoint(x: size.width * CGFloat(i) / CGFloat(pts.count - 1),
                                 y: size.height * (1 - p))
                i == 0 ? path.move(to: pt) : path.addLine(to: pt)
            }
            let up = pts.last! >= pts.first!
            ctx.stroke(path, with: .color(up ? color : BL.breaking),
                       style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))
        }
        .frame(width: width, height: height)
        .accessibilityHidden(true)
    }

    static func points(seed: String) -> [CGFloat] {
        var x: UInt64 = 5381
        for c in seed.unicodeScalars { x = x &* 31 &+ UInt64(c.value) }
        var pts: [CGFloat] = []; var v: CGFloat = 0.5
        for _ in 0..<12 {
            x = x &* 6364136223846793005 &+ 1442695040888963407
            let r = CGFloat(x >> 33) / CGFloat(UInt32.max)
            v = min(0.95, max(0.08, v + (r - 0.42) * 0.3))
            pts.append(v)
        }
        return pts
    }
}

// MARK: - Toast

struct Toast: ViewModifier {
    @Binding var message: String?
    func body(content: Content) -> some View {
        content.overlay(alignment: .bottom) {
            if let message {
                Text(message)
                    .font(.footnote.weight(.medium))
                    .padding(.horizontal, 18).padding(.vertical, 11)
                    .blGlass(in: Capsule())
                    .padding(.bottom, 24)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                    .task {
                        try? await Task.sleep(for: .seconds(2.4))
                        withAnimation(BL.spring) { self.message = nil }
                    }
            }
        }
    }
}

extension View {
    func toast(_ message: Binding<String?>) -> some View { modifier(Toast(message: message)) }
}

// MARK: - Linked text (inline story references → tappable links)

/// Renders prose where story headlines (supplied via `refs`) become tappable links
/// using the custom `descry://story/<id>` scheme. The enclosing view intercepts them
/// with an `OpenURLAction` and pushes the story. See backend `story_refs` / linkify.
struct LinkedText: View {
    let text: String
    var refs: [StoryRef] = []

    var body: some View { Text(attributed) }

    private var attributed: AttributedString {
        var s = AttributedString(text)
        // Longest headlines first so a short headline can't shadow a longer overlap.
        for ref in refs.sorted(by: { $0.headline.count > $1.headline.count })
        where !ref.headline.isEmpty {
            guard let url = URL(string: "descry://story/\(ref.storyID)") else { continue }
            var from = s.startIndex
            while from < s.endIndex, let r = s[from...].range(of: ref.headline) {
                s[r].link = url
                s[r].foregroundColor = BL.accent
                s[r].underlineStyle = .single
                from = r.upperBound
            }
        }
        return s
    }
}

extension View {
    /// Attach to any view containing `LinkedText`: routes `descry://story/<id>` taps
    /// to `open(id)` and leaves normal URLs to the system.
    func onStoryLink(_ open: @escaping (String) -> Void) -> some View {
        environment(\.openURL, OpenURLAction { url in
            guard url.scheme == "descry", url.host == "story" else { return .systemAction }
            open(url.lastPathComponent)
            return .handled
        })
    }
}

// MARK: - Zoom hero transition helpers (iOS 18+, no-op earlier)

extension View {
    @ViewBuilder
    func blZoomSource(id: String, ns: Namespace.ID) -> some View {
        if #available(iOS 18.0, *) { self.matchedTransitionSource(id: id, in: ns) } else { self }
    }
    @ViewBuilder
    func blZoomDestination(id: String, ns: Namespace.ID) -> some View {
        if #available(iOS 18.0, *) { self.navigationTransition(.zoom(sourceID: id, in: ns)) } else { self }
    }
}
