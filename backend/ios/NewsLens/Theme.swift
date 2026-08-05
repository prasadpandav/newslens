import SwiftUI
import Combine

// MARK: - Adaptive color helper (follows the device's light/dark mode)

extension Color {
    init(light: Color, dark: Color) {
        self.init(uiColor: UIColor { traits in
            traits.userInterfaceStyle == .dark ? UIColor(dark) : UIColor(light)
        })
    }

    /// The design tokens are written as hex in the mockups and in the web
    /// portal's CSS. Transcribing them into `Color(red:green:blue:)` decimals
    /// by hand is where a palette silently drifts from its source, so they are
    /// carried across as the same literals.
    static func hex(_ rgb: UInt32) -> Color {
        Color(red:   Double((rgb >> 16) & 0xFF) / 255,
              green: Double((rgb >>  8) & 0xFF) / 255,
              blue:  Double( rgb        & 0xFF) / 255)
    }
    /// Translucent ink (`rgba(23,21,15,a)`) — rules and edges on light paper.
    static func ink(_ alpha: Double) -> Color { hex(0x17150F).opacity(alpha) }
    /// Translucent paper (`rgba(247,244,238,a)`) — the same, on night paper.
    static func paper(_ alpha: Double) -> Color { hex(0xF7F4EE).opacity(alpha) }
}

// MARK: - Skins

/// The three looks, matching the web portal's `data-skin` values and the
/// server's `ContextIn.theme` exactly — the value round-trips through the
/// account context, so these raw values are a cross-platform contract.
enum DescrySkin: String, CaseIterable, Identifiable, Codable {
    case `default`, journal, signal
    var id: String { rawValue }

    var name: String {
        switch self {
        case .default: return "Descry"
        case .journal: return "The Journal"
        case .signal:  return "The Signal"
        }
    }
    var blurb: String {
        switch self {
        case .default: return "The standard look."
        case .journal: return "Warm editorial broadsheet — serif headlines, cream paper, one carmine accent."
        case .signal:  return "Swiss precision instrument — mono numerals, flat white, one vermilion accent."
        }
    }
    /// Swatch shown in the picker, mirroring the web picker's three chips.
    var swatch: [Color] {
        switch self {
        case .default: return [Color(red: 0.302, green: 0.624, blue: 1.0),
                               Color(red: 0.486, green: 0.361, blue: 1.0),
                               Color(red: 0.961, green: 0.969, blue: 0.984)]
        case .journal: return [Color(red: 0.557, green: 0.169, blue: 0.118),
                               Color(red: 0.098, green: 0.082, blue: 0.067),
                               Color(red: 0.961, green: 0.945, blue: 0.910)]
        case .signal:  return [Color(red: 0.847, green: 0.216, blue: 0.110),
                               Color(red: 0.071, green: 0.071, blue: 0.063),
                               Color(red: 0.984, green: 0.984, blue: 0.976)]
        }
    }
    init(storage: String?) { self = DescrySkin(rawValue: storage ?? "") ?? .default }
}

// MARK: - Palette

/// Every colour and shape token the UI draws with, as an *instance* rather than
/// a set of globals.
///
/// This is deliberately not `static let`s on `BL` any more. Statics can't tell
/// SwiftUI that anything changed: a view whose stored properties are unchanged
/// is not re-evaluated, so after a theme switch it keeps rendering the colours
/// it captured the first time — which is precisely the "one page shows the
/// previous theme, another shows something else" symptom. Reading the palette
/// out of the Environment instead makes the dependency explicit, so SwiftUI
/// invalidates exactly the views that draw with it, all in the same frame.
struct Palette {
    var skin: DescrySkin = .default

    var ink: Color, ink2: Color, surface: Color, surface2: Color
    var hairline: Color, hairline2: Color
    var text: Color, text2: Color
    var accent: Color, ai: Color
    var trust: Color, warning: Color, breaking: Color, prediction: Color

    // MARK: Redesign tokens
    //
    // The paper design splits what used to be two text tiers into four, and —
    // the part that is easy to miss — splits each verdict colour by ROLE. In
    // the mockups `#A9741C` appears 41 times as a fill and never once as
    // `color:`; the text beside those fills is `#8A5A18`. A hue bright enough
    // to read as a 5px pip is not legible as 11px type, so `warning` (text) and
    // `midFill` (pip) are deliberately different values rather than one colour
    // used twice. Same for good and bad.

    /// Third text tier — the prose inside a card, one step quieter than `text2`.
    /// A distinct token because the mockups use `#4A4640` for a lead card's
    /// paragraph and `#5C574F` for the smaller cards and the explainer notes.
    var text3: Color = .secondary
    /// Fourth tier — captions, meta lines. Between `text3` and `faint`.
    var mute: Color = .secondary
    /// Fifth tier — the quietest type that still has to be readable.
    var faint: Color = .secondary
    /// Non-text greys: separator dots, empty pips, disabled marks.
    var ghost: Color = .secondary.opacity(0.5)
    /// The "Why this matters to you" panel: warm fill, its edge, its label, and
    /// the body text inside it — which is darker than `text2`, because it sits
    /// on sand rather than paper.
    var sand: Color = .clear, sandEdge: Color = .clear, sandInk: Color = .primary
    var sandText: Color = .primary
    /// Fill-only variants of the three verdict colours (pips, bars, ticks).
    var goodFill: Color = .green, midFill: Color = .orange, badFill: Color = .red

    /// Corner radius for content cards. The two editorial skins are near-square;
    /// Signal is fully square, which is one of its stated design goals.
    var radius: CGFloat = 18
    /// Journal is serif-led; the others are the system sans.
    var serifBody: Bool = false
    /// Ambient background glows belong to the default skin only — "nothing
    /// glows" is the whole point of both editorial skins.
    var ambientGlow: Bool = true
    /// Shadows under cards, likewise.
    var cardShadow: Bool = true

    var aiGradient: LinearGradient {
        // Both editorial skins are single-accent by design, so what is a
        // two-stop gradient in the default skin collapses to a flat fill
        // rather than inventing a second hue they don't have.
        LinearGradient(colors: skin == .default ? [accent, ai] : [accent, accent],
                       startPoint: .topLeading, endPoint: .bottomTrailing)
    }

    /// The verdict colour for TYPE. Breakpoints are the agreement scale's, not
    /// their own set — see `AgreementBand`, which owns the thresholds and the
    /// words. Two ladders that drift apart would let a row read "Some sources
    /// agree" in the colour of "Most".
    func credColor(_ score: Double) -> Color {
        switch AgreementBand.of(score).tone {
        case .good: return trust
        case .mid:  return warning
        case .bad:  return breaking
        }
    }

    /// The same verdict as a FILL — pips, bars, ticks. Brighter than
    /// `credColor`, which is why the two exist.
    func credFill(_ score: Double) -> Color {
        switch AgreementBand.of(score).tone {
        case .good: return goodFill
        case .mid:  return midFill
        case .bad:  return badFill
        }
    }

    // MARK: Typography
    //
    // The design names three families: Newsreader for headlines and prose, IBM
    // Plex Sans for the interface, IBM Plex Mono for figures and labels. None
    // ship with iOS, so each maps to the system face closest in intent — New
    // York (`.serif`), SF Pro, SF Mono. Sizes are the mockups' own pixel values;
    // SwiftUI scales `Font.system(size:)` with Dynamic Type, so they still grow
    // with the reader's text-size setting.

    /// Headlines and body prose.
    func serif(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .serif)
    }
    /// Interface text — buttons, labels, meta lines.
    func sans(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight)
    }
    /// Figures, kickers and the small capitalised labels.
    func mono(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }

    /// Scale a hand-tuned corner radius to the skin. Views carry radii chosen for
    /// the default look (10, 12, 14…); passing them through here is what stops
    /// Signal — whose whole premise is zero radius — from rendering rounded
    /// buttons and panels next to its square cards.
    func r(_ designed: CGFloat) -> CGFloat {
        switch skin {
        case .default: return designed
        case .journal: return min(designed, 3)
        case .signal:  return 0
        }
    }

    /// An accent glow, or nothing on the skins that don't glow.
    func glow(_ color: Color, _ opacity: Double) -> Color {
        cardShadow ? color.opacity(opacity) : .clear
    }

    /// Body/headline font honouring the skin's typographic intent.
    func font(_ style: Font.TextStyle, weight: Font.Weight? = nil) -> Font {
        let f = serifBody ? Font.system(style, design: .serif) : Font.system(style)
        return weight.map { f.weight($0) } ?? f
    }

    // MARK: Skin definitions

    /// Descry — ink on paper. The same token set the web portal's `.fx`/`.rx`
    /// pages declare, value for value, so a story looks like itself on both.
    /// Light/dark adaptive: the design has a night paper (`#12110F`), it is not
    /// a light-only look.
    static let descry = Palette(
        skin: .default,
        // --paper / --card
        ink:  Color(light: .hex(0xF7F4EE), dark: .hex(0x12110F)),
        ink2: Color(light: .hex(0xFFFDF9), dark: .hex(0x171512)),
        surface:  Color(light: .hex(0xFFFDF9), dark: .hex(0x171512)),
        // --sand: the tint behind "Why this matters to you" and other insets.
        surface2: Color(light: .hex(0xEFE9DC), dark: .hex(0x1E1B16)),
        // --rule / --rule-2
        hairline:  Color(light: .ink(0.09), dark: .paper(0.12)),
        hairline2: Color(light: .ink(0.14), dark: .paper(0.18)),
        // --ink / --ink-2
        text:  Color(light: .hex(0x17150F), dark: .hex(0xF2EEE6)),
        text2: Color(light: .hex(0x4A4640), dark: Color.hex(0xF2EEE6).opacity(0.82)),
        // --indigo: the one link colour. There is no second accent hue in this
        // design, so `ai` is not a violet any more — it is the same indigo, and
        // `aiGradient` collapses to a flat fill (see the initialiser's note).
        accent: Color(light: .hex(0x2E4A7D), dark: .hex(0x9DB4DC)),
        ai:     Color(light: .hex(0x2E4A7D), dark: .hex(0x9DB4DC)),
        // --good-ink / --mid-ink / --bad-ink. These are the TEXT variants; the
        // brighter fills are `goodFill`/`midFill`/`badFill` below.
        trust:    Color(light: .hex(0x3F6B4A), dark: .hex(0x8FBA98)),
        warning:  Color(light: .hex(0x8A5A18), dark: .hex(0xE2B96A)),
        breaking: Color(light: .hex(0x9B3F30), dark: .hex(0xD8927D)),
        // Forecasts are drawn in the same ink as everything else — the design
        // has no purple, and a lone violet on paper reads as a different app.
        prediction: Color(light: .hex(0x2E4A7D), dark: .hex(0x9DB4DC)),
        text3: Color(light: .hex(0x5C574F), dark: Color.hex(0xF2EEE6).opacity(0.72)),
        mute:  Color(light: .hex(0x7A756C), dark: .paper(0.62)),
        faint: Color(light: .hex(0x6F6A61), dark: .paper(0.55)),
        ghost: Color(light: .hex(0xC0BAB0), dark: .paper(0.25)),
        sand:     Color(light: .hex(0xEFE9DC), dark: .hex(0x1E1B16)),
        sandEdge: .hex(0xC8B98F),
        // #7A6534 — the label on a sand panel, and the "your lens" / "near you"
        // pill text. Darker than the old #8A7A4E, which the mockups only use
        // for one strength label and never for a heading.
        sandInk:  Color(light: .hex(0x7A6534), dark: .hex(0xC8B98F)),
        sandText: Color(light: .hex(0x3A342A), dark: .paper(0.85)),
        goodFill: Color(light: .hex(0x3F6B4A), dark: .hex(0x7AA884)),
        midFill:  Color(light: .hex(0xA9741C), dark: .hex(0xD2A54F)),
        badFill:  Color(light: .hex(0x9B3F30), dark: .hex(0xC87F6A)),
        // Paper doesn't glow and doesn't float: the cards are ruled, not raised.
        radius: 10, serifBody: false, ambientGlow: false, cardShadow: false)

    /// "The Journal" — ink on cream paper, one carmine accent. A fixed palette,
    /// not a light/dark pair: a picked skin is a deliberate look, which is the
    /// same call the web makes by letting :root[data-skin] beat the dark-mode
    /// block. Trust/warning stay inked; an editorial page signals "fine" with
    /// restraint rather than a green light.
    static let journal = Palette(
        skin: .journal,
        ink:  Color(red: 0.961, green: 0.945, blue: 0.910),
        ink2: Color(red: 0.937, green: 0.918, blue: 0.867),
        surface:  Color(red: 0.098, green: 0.082, blue: 0.067).opacity(0.035),
        surface2: Color(red: 0.098, green: 0.082, blue: 0.067).opacity(0.06),
        hairline:  Color(red: 0.788, green: 0.757, blue: 0.698),
        hairline2: Color(red: 0.098, green: 0.082, blue: 0.067),
        text:  Color(red: 0.098, green: 0.082, blue: 0.067),
        text2: Color(red: 0.361, green: 0.329, blue: 0.290),
        accent: Color(red: 0.557, green: 0.169, blue: 0.118),
        ai:     Color(red: 0.557, green: 0.169, blue: 0.118),
        trust:    Color(red: 0.098, green: 0.082, blue: 0.067),
        warning:  Color(red: 0.361, green: 0.329, blue: 0.290),
        breaking: Color(red: 0.557, green: 0.169, blue: 0.118),
        prediction: Color(red: 0.098, green: 0.082, blue: 0.067),
        // Journal is a single-ink look, so its four text tiers are one hue at
        // four weights rather than four hues. Its verdict FILLS are the only
        // place it admits colour beyond carmine, and only because an all-ink
        // pip row cannot say which of five pips are lit.
        mute:  Color(red: 0.361, green: 0.329, blue: 0.290),
        faint: Color(red: 0.451, green: 0.420, blue: 0.376),
        ghost: Color(red: 0.788, green: 0.757, blue: 0.698),
        sand:     Color(red: 0.937, green: 0.918, blue: 0.867),
        sandEdge: Color(red: 0.784, green: 0.725, blue: 0.561),
        sandInk:  Color(red: 0.361, green: 0.329, blue: 0.290),
        goodFill: Color(red: 0.098, green: 0.082, blue: 0.067),
        midFill:  Color(red: 0.451, green: 0.420, blue: 0.376),
        badFill:  Color(red: 0.557, green: 0.169, blue: 0.118),
        radius: 3, serifBody: true, ambientGlow: false, cardShadow: false)

    /// "The Signal" — Swiss instrument. Zero radius, zero shadow, zero gradient;
    /// vermilion reserved for alert states, everything else ink or grey.
    static let signal = Palette(
        skin: .signal,
        ink:  Color(red: 0.984, green: 0.984, blue: 0.976),
        ink2: .white,
        surface:  Color(red: 0.071, green: 0.071, blue: 0.063).opacity(0.03),
        surface2: Color(red: 0.071, green: 0.071, blue: 0.063).opacity(0.05),
        hairline:  Color(red: 0.867, green: 0.863, blue: 0.831),
        hairline2: Color(red: 0.071, green: 0.071, blue: 0.063),
        text:  Color(red: 0.071, green: 0.071, blue: 0.063),
        text2: Color(red: 0.431, green: 0.427, blue: 0.400),
        accent: Color(red: 0.847, green: 0.216, blue: 0.110),
        ai:     Color(red: 0.847, green: 0.216, blue: 0.110),
        trust:    Color(red: 0.071, green: 0.071, blue: 0.063),
        warning:  Color(red: 0.431, green: 0.427, blue: 0.400),
        breaking: Color(red: 0.847, green: 0.216, blue: 0.110),
        prediction: Color(red: 0.071, green: 0.071, blue: 0.063),
        // Signal holds vermilion back for alerts, so a lit pip is ink and an
        // unlit one is the grey track — the count is read from position, not hue.
        mute:  Color(red: 0.431, green: 0.427, blue: 0.400),
        faint: Color(red: 0.529, green: 0.525, blue: 0.494),
        ghost: Color(red: 0.867, green: 0.863, blue: 0.831),
        sand:     Color(red: 0.071, green: 0.071, blue: 0.063).opacity(0.05),
        sandEdge: Color(red: 0.071, green: 0.071, blue: 0.063),
        sandInk:  Color(red: 0.071, green: 0.071, blue: 0.063),
        goodFill: Color(red: 0.071, green: 0.071, blue: 0.063),
        midFill:  Color(red: 0.431, green: 0.427, blue: 0.400),
        badFill:  Color(red: 0.847, green: 0.216, blue: 0.110),
        radius: 0, serifBody: false, ambientGlow: false, cardShadow: false)

    static func of(_ skin: DescrySkin) -> Palette {
        switch skin {
        case .default: return .descry
        case .journal: return .journal
        case .signal:  return .signal
        }
    }
}

extension EnvironmentValues {
    /// Read with `@Environment(\.palette) private var pal`.
    var palette: Palette {
        get { self[PaletteKey.self] }
        set { self[PaletteKey.self] = newValue }
    }
}
private struct PaletteKey: EnvironmentKey {
    static let defaultValue = Palette.descry
}

/// Re-asserts the current skin's palette from the ThemeStore.
///
/// The root injection covers the whole tab tree, but a `.sheet` is presented
/// from its own hosting controller. Environment propagation into sheets is
/// reliable in current SwiftUI, and this codebase already re-passes
/// `.environmentObject(api)` at every sheet for the same reason — so a sheet
/// that missed the palette would silently fall back to `Palette.descry` and
/// show the default look on top of a themed app, which is precisely the
/// "one screen is a different theme" failure being fixed. Cheap insurance.
struct Skinned: ViewModifier {
    @EnvironmentObject private var theme: ThemeStore
    func body(content: Content) -> some View {
        content.environment(\.palette, theme.palette)
    }
}

extension View {
    func skinned() -> some View { modifier(Skinned()) }
}

// MARK: - Theme store

/// Owns the chosen skin. Persisted locally so the very first frame after launch
/// is already the right colour (no flash of the default look), then reconciled
/// with the account, which is the source of truth across devices.
@MainActor
final class ThemeStore: ObservableObject {
    static let shared = ThemeStore()
    private static let key = "bl_skin"

    @Published private(set) var skin: DescrySkin = .default

    private init() {
        skin = DescrySkin(storage: UserDefaults.standard.string(forKey: Self.key))
    }

    var palette: Palette { .of(skin) }

    /// Local-only switch — instant, no round trip. Used for the picker's
    /// immediate feedback and to fall back on sign-out.
    func set(_ new: DescrySkin) {
        guard new != skin else { return }
        skin = new
        UserDefaults.standard.set(new.rawValue, forKey: Self.key)
    }

    /// Adopt whatever this account saved elsewhere (web, another device).
    func adopt(fromContext theme: String?) { set(DescrySkin(storage: theme)) }

    func reset() { set(.default) }
}

// MARK: - Non-visual tokens

/// What is left of the old global token bag. The colours that used to live here
/// are gone on purpose: they are now `Palette` values read from the Environment,
/// so a skin change invalidates the views that draw with them. Anything that is
/// genuinely skin-independent can still live here.
enum BL {
    static let spring = Animation.spring(response: 0.45, dampingFraction: 0.85)
}

// MARK: - Background (ambient radial glows; adapts to mode)

struct InkBackground: View {
    @Environment(\.colorScheme) private var scheme
    @Environment(\.palette) private var pal

    var body: some View {
        ZStack {
            pal.ink.ignoresSafeArea()
            // Both editorial skins exist to get away from a floating glow, so
            // the ambient radials are the default skin's alone.
            if pal.ambientGlow {
                RadialGradient(colors: [pal.accent.opacity(scheme == .dark ? 0.13 : 0.07), .clear],
                               center: .init(x: 0.85, y: -0.05), startRadius: 0, endRadius: 420)
                RadialGradient(colors: [pal.ai.opacity(scheme == .dark ? 0.10 : 0.05), .clear],
                               center: .init(x: 0.05, y: 1.05), startRadius: 0, endRadius: 380)
            }
        }
        .ignoresSafeArea()
    }
}

// MARK: - Liquid Glass with graceful fallback (functional layer only)

/// These are `ViewModifier`s rather than plain `View` extension methods because
/// a method on `View` has nowhere to read `@Environment` from — it would have to
/// close over a global, which is exactly the staleness this refactor removes.
private struct BLGlass<S: Shape>: ViewModifier {
    let shape: S
    @Environment(\.palette) private var pal

    @ViewBuilder
    func body(content: Content) -> some View {
        // Paper doesn't blur: the editorial skins take the opaque path even on
        // iOS 26, where the default skin gets Liquid Glass.
        if #available(iOS 26.0, *), pal.skin == .default {
            content.glassEffect(.regular, in: shape)
        } else if pal.skin == .default {
            content.background(.ultraThinMaterial, in: shape)
                .overlay(shape.stroke(pal.hairline2, lineWidth: 1))
        } else {
            content.background(pal.ink2, in: shape)
                .overlay(shape.stroke(pal.hairline2, lineWidth: 1))
        }
    }
}

private struct BLCard: ViewModifier {
    var radius: CGFloat?
    @Environment(\.palette) private var pal

    func body(content: Content) -> some View {
        // An explicitly passed radius is a value tuned for the default look, so
        // it goes through the skin's scale too rather than straight to the shape.
        let r = radius.map(pal.r) ?? pal.radius
        return content
            .background(
                RoundedRectangle(cornerRadius: r, style: .continuous)
                    .fill(pal.surface)
                    .overlay(RoundedRectangle(cornerRadius: r, style: .continuous)
                        .stroke(pal.hairline, lineWidth: 1)))
            .shadow(color: pal.cardShadow ? Color(light: .black.opacity(0.06), dark: .clear) : .clear,
                    radius: pal.cardShadow ? 10 : 0, y: pal.cardShadow ? 4 : 0)
    }
}

extension View {
    /// iOS 26 Liquid Glass on the functional layer; material fallback earlier.
    func blGlass(in shape: some Shape = Capsule()) -> some View {
        modifier(BLGlass(shape: shape))
    }

    /// Content-layer card: opaque surface (never glass, per HIG guidance).
    /// Passing no radius takes the skin's own corner treatment.
    func blCard(radius: CGFloat? = nil) -> some View {
        modifier(BLCard(radius: radius))
    }
}

// MARK: - Chip

struct Chip: View {
    var text: String
    /// Nil means "the skin's secondary text colour" — resolved in `body`, since
    /// a stored property's default value can't read the Environment.
    var color: Color?
    var filled: Bool = false
    @Environment(\.palette) private var pal

    var body: some View {
        let c = color ?? pal.text2
        // The paper design's chip is an outline that fills solid when it is on:
        // a low-alpha tint is not a strong enough "selected" state at 12.5px,
        // and it was the thing the topic filter was reported for. Journal keeps
        // the same rule; the pill just squares off.
        return Text(text)
            .font(pal.sans(13.5, filled ? .medium : .regular))
            .padding(.horizontal, 13).padding(.vertical, 7)
            .background(shape.fill(filled ? c : .clear))
            .overlay(shape.stroke(filled ? c : pal.hairline2, lineWidth: 1))
            .foregroundStyle(filled ? pal.ink : pal.text2)
            // The whole pill is the target, not just the glyphs. Without this
            // the unfilled chip's fill is `.clear` and therefore not
            // hit-testable, so taps landing in the padding do nothing.
            .contentShape(shape)
    }

    private var shape: AnyShape {
        pal.radius == 0 ? AnyShape(Rectangle()) : AnyShape(Capsule())
    }
}

// MARK: - The Descry mark
//
// One ring, one dot — an aperture. To descry is to make something out at a
// distance, which is what the product does to a story.
//
// It exists as a view rather than as three hand-drawn ZStacks because it was
// drawn ad hoc in the feed masthead and nowhere else, so four of the five tabs
// carried no identity at all and the app icon was still a blue-and-violet
// gradient sparkle from before the redesign. The same ratios are in
// `web/icon.svg`, `.logo .mark` in web/index.html, and
// backend/ios/AppIcon/make_icon.py — change one and the others are wrong.
//
// Ratios are the design's own 18pt mark: the stroke is a twelfth of the outer
// diameter, the dot a third of it. Takes the surrounding foreground colour, so
// it inverts on a dark panel without a second asset.

struct DescryMark: View {
    var size: CGFloat = 18

    var body: some View {
        ZStack {
            Circle().strokeBorder(lineWidth: size / 12)
            Circle().frame(width: size / 3, height: size / 3)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)   // the wordmark or page title names it
    }
}

/// Mark plus wordmark. The masthead lockup — the feed's, and the only place the
/// word is set, because repeating it on all five tabs reads as branding rather
/// than as a masthead.
struct DescryLockup: View {
    @Environment(\.palette) private var pal
    var size: CGFloat = 18

    var body: some View {
        HStack(spacing: size * 0.44) {
            DescryMark(size: size)
            Text("DESCRY")
                .font(pal.serif(size * 0.78, .medium))
                .kerning(size * 0.109)      // .14em
        }
        .foregroundStyle(pal.text)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Descry")
        .accessibilityAddTraits(.isHeader)
    }
}

/// The header every tab except the feed wears: the mark, the page's own name in
/// the editorial serif, a counted line under it, and whatever the screen needs
/// on the right. One rule under it, as the design draws every page head.
struct PageHeader<Trailing: View>: View {
    @Environment(\.palette) private var pal

    let title: String
    var subtitle: String? = nil
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 9) {
                    DescryMark(size: 15)
                        .foregroundStyle(pal.text)
                    Text(title)
                        .font(pal.serif(24))
                        .foregroundStyle(pal.text)
                        .accessibilityAddTraits(.isHeader)
                }
                if let subtitle {
                    Text(subtitle)
                        .font(pal.mono(13))
                        .foregroundStyle(pal.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            trailing
        }
        .padding(.horizontal, 20)
        .padding(.top, 4)
        .padding(.bottom, 14)
        .overlay(alignment: .bottom) { Rectangle().fill(pal.hairline).frame(height: 1) }
    }
}

extension PageHeader where Trailing == EmptyView {
    init(title: String, subtitle: String? = nil) {
        self.init(title: title, subtitle: subtitle) { EmptyView() }
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
    @Environment(\.palette) private var pal

    var body: some View {
        if let rel = LastTold.relative(at) {
            Text(rel)
                .font(.caption2)
                .foregroundStyle(pal.text2)
                .accessibilityLabel(Text("Last told \(LastTold.full(at) ?? rel)"))
        }
    }
}

// MARK: - Trust / corroboration ring

struct TrustRing: View {
    var score: Double
    var size: CGFloat = 46
    @State private var animated = false
    @Environment(\.palette) private var pal

    var body: some View {
        ZStack {
            // Was Color.white.opacity(0.09) — a white track is invisible on a
            // white card, so in light mode the ring had no groove at all and on
            // paper it was worse. The neutral track has to come from the skin.
            Circle().stroke(pal.surface2, lineWidth: 4.5)
            Circle()
                .trim(from: 0, to: animated ? score / 100 : 0)
                .stroke(pal.credFill(score),
                        style: StrokeStyle(lineWidth: 4.5,
                                           lineCap: pal.radius == 0 ? .butt : .round))
                .rotationEffect(.degrees(-90))
            Text("\(Int(score))")
                .font(.system(size: size * 0.3, weight: .bold, design: .monospaced))
                .foregroundStyle(pal.credColor(score))
        }
        .frame(width: size, height: size)
        .onAppear { withAnimation(BL.spring.delay(0.2)) { animated = true } }
        .accessibilityLabel("Corroboration \(Int(score)) percent")
    }
}

// MARK: - Trust meter bar

struct TrustMeter: View {
    var score: Double

    /// The agreement scale's own sentence — "Most sources agree", not "Highly
    /// corroborated". The design bans that vocabulary outright, and a screen
    /// still using it beside one that doesn't reads as two different products.
    /// Screens redesigned onto the paper layout use `AgreementLine` instead;
    /// this keeps the rest of the app saying the same thing in the meantime.
    private var label: String { AgreementBand.of(score).name }
    @Environment(\.palette) private var pal

    var body: some View {
        HStack(spacing: 8) {
            Text(label)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(pal.credColor(score))
                .lineLimit(1)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    // Same bug as TrustRing's track: a hardcoded white bar is
                    // invisible in light mode and on both paper skins.
                    bar.fill(pal.surface2)
                    // Fill, not text colour — a bar is a fill. See Palette.
                    bar.fill(pal.credFill(score))
                        .frame(width: geo.size.width * score / 100)
                }
            }
            .frame(height: 5)
            Text("\(Int(score))%")
                .font(.caption2.weight(.semibold).monospaced())
                .foregroundStyle(pal.credColor(score))
        }
        .accessibilityElement(children: .combine)
    }

    private var bar: AnyShape {
        pal.radius == 0 ? AnyShape(Rectangle()) : AnyShape(Capsule())
    }
}

// MARK: - Impact badge

struct ImpactBadge: View {
    var score: Int
    @Environment(\.palette) private var pal
    private var label: String { ["", "For you", "Affects you", "High impact"][min(score, 3)] }
    private var color: Color { [pal.text2, pal.accent, pal.warning, pal.breaking][min(score, 3)] }
    var body: some View {
        if score > 0 { Chip(text: label, color: color, filled: true) }
    }
}

// MARK: - Sparkline (deterministic from a seed string, drawn with Canvas)

struct Sparkline: View {
    var seed: String
    /// Nil takes the skin's accent — a stored default can't read the Environment.
    var color: Color?
    var width: CGFloat = 72
    var height: CGFloat = 22
    @Environment(\.palette) private var pal

    var body: some View {
        let pts = Self.points(seed: seed)
        let stroke = color ?? pal.accent
        let down = pal.breaking
        Canvas { ctx, size in
            var path = Path()
            for (i, p) in pts.enumerated() {
                let pt = CGPoint(x: size.width * CGFloat(i) / CGFloat(pts.count - 1),
                                 y: size.height * (1 - p))
                i == 0 ? path.move(to: pt) : path.addLine(to: pt)
            }
            let up = pts.last! >= pts.first!
            ctx.stroke(path, with: .color(up ? stroke : down),
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
    @Environment(\.palette) private var pal

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
                s[r].foregroundColor = pal.accent
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
