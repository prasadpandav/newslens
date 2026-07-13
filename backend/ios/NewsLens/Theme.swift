import SwiftUI

// MARK: - Bluelligent Native design tokens

enum BL {
    // Palette (dark-first ink + semantic colors)
    static let ink        = Color(red: 0.027, green: 0.043, blue: 0.078)   // #070B14
    static let ink2       = Color(red: 0.043, green: 0.067, blue: 0.125)   // #0B1120
    static let surface    = Color.white.opacity(0.045)
    static let surface2   = Color.white.opacity(0.07)
    static let hairline   = Color.white.opacity(0.08)
    static let hairline2  = Color.white.opacity(0.14)
    static let text2      = Color(red: 0.604, green: 0.647, blue: 0.722)   // #9AA5B8
    static let accent     = Color(red: 0.302, green: 0.624, blue: 1.0)     // #4D9FFF
    static let ai         = Color(red: 0.486, green: 0.361, blue: 1.0)     // #7C5CFF
    static let trust      = Color(red: 0.239, green: 0.863, blue: 0.592)   // #3DDC97
    static let warning    = Color(red: 1.0,   green: 0.761, blue: 0.302)   // #FFC24D
    static let breaking   = Color(red: 1.0,   green: 0.365, blue: 0.451)   // #FF5D73
    static let prediction = Color(red: 0.706, green: 0.549, blue: 1.0)     // #B48CFF

    static let aiGradient = LinearGradient(colors: [accent, ai],
                                           startPoint: .topLeading, endPoint: .bottomTrailing)
    static let spring = Animation.spring(response: 0.45, dampingFraction: 0.85)

    static func credColor(_ score: Double) -> Color {
        score >= 75 ? trust : score >= 50 ? warning : breaking
    }
}

// MARK: - Background (ambient radial glows over ink)

struct InkBackground: View {
    var body: some View {
        ZStack {
            BL.ink.ignoresSafeArea()
            RadialGradient(colors: [BL.accent.opacity(0.13), .clear],
                           center: .init(x: 0.85, y: -0.05), startRadius: 0, endRadius: 420)
            RadialGradient(colors: [BL.ai.opacity(0.10), .clear],
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

    /// Content-layer card: opaque-dark surface (never glass, per HIG guidance).
    func blCard(radius: CGFloat = 18) -> some View {
        self.background(
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .fill(BL.surface)
                .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .stroke(BL.hairline, lineWidth: 1)))
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
    var body: some View {
        HStack(spacing: 8) {
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
