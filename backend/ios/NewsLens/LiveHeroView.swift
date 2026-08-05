import SwiftUI
import Combine

/// The dynamic hero at the top of the home screen (features 2, 3, 5).
///
/// Deliberately compact and calm: one card at a time in a short fixed height, a
/// slow 6s auto-advance (disabled under Reduce Motion), and a gentle cross-fade.
/// Breaking cards are pinned first so genuine urgency always takes over (feature 3).
/// The gear opens `LiveConfigView` to choose categories and order (feature 5).
struct LiveHeroView: View {
    @Environment(\.palette) private var pal

    @ObservedObject var stream: LiveStream
    @Binding var prefs: LivePrefs
    var onPrefsChanged: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var index = 0
    @State private var showConfig = false
    private let tick = Timer.publish(every: 6, on: .main, in: .common).autoconnect()

    /// The strip is a FIXED height, and this is why it matters far more than it
    /// looks. Cards differ: one title wraps to two lines and the next does not,
    /// one carries a "BREAKING" flag and a subtitle and the next carries
    /// neither. Sized to their content, the strip changed height every time it
    /// auto-advanced — and it sits above the greeting, the topic chips and the
    /// lead story, so the entire feed slid up and down by ~18pt every six
    /// seconds, animated.
    ///
    /// Measured: the topic chip row moved from y=419.7 to y=401.3 on its own,
    /// 10s after the feed painted. That is the whole of "the chips work
    /// sometimes and sometimes the hero story opens instead" — the chip is
    /// simply not where the finger is going by the time the touch lands, and
    /// what slides into its place is the hero story's tap target.
    private static let cardHeight: CGFloat = 76
    private static let dotsHeight: CGFloat = 5

    /// Cards allowed by the user's config, ordered breaking → events → scores → markets.
    private var visible: [LiveCard] {
        let allowed = Set(prefs.categories)
        let rank = ["breaking": 0, "event": 1, "score": 2, "market": 3]
        return stream.cards
            .filter { c in c.category.map { allowed.contains($0.rawValue) } ?? false }
            .sorted { (rank[$0.type] ?? 9, -$0.priority) < (rank[$1.type] ?? 9, -$1.priority) }
    }

    var body: some View {
        if prefs.enabled, !visible.isEmpty {
            let cards = visible
            let current = cards[min(index, cards.count - 1)]
            VStack(alignment: .leading, spacing: 8) {
                header
                heroCard(current)
                    .frame(height: Self.cardHeight)
                    .id(current.id)   // drives the cross-fade between cards
                    .transition(.opacity)
                // Always laid out, so the strip's height does not depend on how
                // many cards happen to be live at this instant.
                dots(cards.count)
                    .frame(height: Self.dotsHeight)
                    .opacity(cards.count > 1 ? 1 : 0)
            }
            .padding(.top, 2)
            .onReceive(tick) { _ in advance(cards.count) }
            .onChange(of: cards.first?.id) {
                // A breaking card just arrived / moved to front → surface it now.
                if cards.first?.type == "breaking" { withAnimation(BL.spring) { index = 0 } }
            }
            .onChange(of: cards.count) { if index >= cards.count { index = 0 } }
            .sheet(isPresented: $showConfig) {
                LiveConfigView(prefs: prefs) { updated in
                    prefs = updated
                    onPrefsChanged()
                }
                .skinned()
            }
        }
    }

    private var header: some View {
        HStack(spacing: 7) {
            Circle()
                .fill(stream.connected ? pal.breaking : pal.text2)
                .frame(width: 7, height: 7)
                .modifier(PulseIfLive(active: stream.connected && !reduceMotion))
            Text(stream.connected ? "LIVE" : "LIVE · reconnecting")
                .font(.caption2.weight(.bold)).kerning(1)
                .foregroundStyle(pal.text2)
            Spacer()
            Button { showConfig = true } label: {
                Image(systemName: "slider.horizontal.3")
                    .font(.caption).foregroundStyle(pal.text2)
            }
            .accessibilityLabel("Configure live section")
        }
    }

    @ViewBuilder
    private func heroCard(_ card: LiveCard) -> some View {
        if let sid = card.storyID, !sid.isEmpty {
            NavigationLink(value: card) { LiveHeroCard(card: card) }
                .buttonStyle(.plain)
        } else {
            LiveHeroCard(card: card)   // score/market with no story → not tappable
        }
    }

    private func dots(_ count: Int) -> some View {
        HStack(spacing: 5) {
            ForEach(0..<count, id: \.self) { i in
                Capsule()
                    .fill(i == min(index, count - 1) ? AnyShapeStyle(pal.accent)
                                                     : AnyShapeStyle(pal.surface2))
                    .frame(width: i == min(index, count - 1) ? 16 : 5, height: 5)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private func advance(_ count: Int) {
        guard !reduceMotion, count > 1 else { return }
        withAnimation(BL.spring) { index = (index + 1) % count }
    }
}

/// A pulsing dot for the LIVE indicator (respects Reduce Motion via `active`).
private struct PulseIfLive: ViewModifier {
    let active: Bool
    @State private var on = false
    func body(content: Content) -> some View {
        content
            .opacity(active ? (on ? 0.35 : 1) : 1)
            .onAppear {
                guard active else { return }
                withAnimation(.easeInOut(duration: 1).repeatForever(autoreverses: true)) {
                    on = true
                }
            }
    }
}

/// One hero card. Colour + icon come from the card's category.
struct LiveHeroCard: View {
    @Environment(\.palette) private var pal

    let card: LiveCard

    private var tint: Color {
        switch card.type {
        case "breaking": return pal.breaking
        case "event":    return pal.warning
        case "score":    return pal.accent
        case "market":   return pal.trust
        default:         return pal.accent
        }
    }
    private var icon: String { card.category?.icon ?? "dot.radiowaves.left.and.right" }

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(tint)
                .frame(width: 34, height: 34)
                .background(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous)
                    .fill(tint.opacity(0.14)))
            VStack(alignment: .leading, spacing: 2) {
                if card.type == "breaking" {
                    Text("BREAKING").font(.caption2.weight(.bold)).kerning(1)
                        .foregroundStyle(pal.breaking)
                }
                // Exactly two lines, always: `lineLimit(2)` alone lets a short
                // title render one line tall and take 18pt out of the strip.
                Text(card.title)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(card.type == "breaking" ? 1 : 2, reservesSpace: true)
                    .multilineTextAlignment(.leading)
                Text(card.subtitle)
                    .font(.caption2).foregroundStyle(pal.text2)
                    .lineLimit(1, reservesSpace: true)
                    .opacity(card.subtitle.isEmpty ? 0 : 1)
            }
            Spacer(minLength: 0)
            if card.storyID?.isEmpty == false {
                Image(systemName: "chevron.right")
                    .font(.caption2).foregroundStyle(pal.text2)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 9)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: pal.r(14), style: .continuous)
            .fill(pal.surface)
            .overlay(RoundedRectangle(cornerRadius: pal.r(14), style: .continuous)
                .stroke(tint.opacity(card.type == "breaking" ? 0.5 : 0.18), lineWidth: 1)))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(card.category?.label ?? "Live"): \(card.title). \(card.subtitle)")
    }
}

// MARK: - Config sheet (feature 5)

/// Choose which categories the hero shows, and their priority order. The list
/// order IS the priority; toggles switch a category on/off. Saved to `live_prefs`.
struct LiveConfigView: View {
    @Environment(\.palette) private var pal

    var prefs: LivePrefs
    var onSave: (LivePrefs) -> Void

    @EnvironmentObject private var api: APIClient
    @Environment(\.dismiss) private var dismiss
    @State private var enabled: Bool
    @State private var rows: [LiveCategory]
    @State private var on: Set<LiveCategory>

    init(prefs: LivePrefs, onSave: @escaping (LivePrefs) -> Void) {
        self.prefs = prefs
        self.onSave = onSave
        _enabled = State(initialValue: prefs.enabled)
        // Enabled categories first (in saved order), then the remaining, disabled.
        let onCats = prefs.categories.compactMap { LiveCategory(rawValue: $0) }
        let rest = LiveCategory.allCases.filter { !onCats.contains($0) }
        _rows = State(initialValue: onCats + rest)
        _on = State(initialValue: Set(onCats))
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Toggle("Show live section", isOn: $enabled)
                        .tint(pal.accent)
                } footer: {
                    Text("A calm, auto-updating strip at the top of your home screen. Breaking news always takes priority.")
                }
                Section {
                    ForEach(rows) { cat in categoryRow(cat) }
                        .onMove { rows.move(fromOffsets: $0, toOffset: $1) }
                } header: {
                    Text("What to show — drag to set priority")
                }
                .disabled(!enabled)
            }
            .environment(\.editMode, .constant(.active))   // always draggable
            .navigationTitle("Live section")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { save() }.fontWeight(.semibold)
                }
            }
        }
    }

    @ViewBuilder
    private func categoryRow(_ cat: LiveCategory) -> some View {
        let isOn = on.contains(cat)
        HStack {
            Label(cat.label, systemImage: cat.icon)
                .foregroundStyle(isOn ? Color.primary : pal.text2)
            Spacer()
            Toggle("", isOn: Binding(
                get: { on.contains(cat) },
                set: { if $0 { on.insert(cat) } else { on.remove(cat) } }))
                .labelsHidden().tint(pal.accent)
        }
    }

    private func save() {
        // Priority order = row order, keeping only enabled categories.
        let cats = rows.filter { on.contains($0) }.map(\.rawValue)
        let updated = LivePrefs(enabled: enabled, categories: cats)
        onSave(updated)
        Task { await api.saveLivePrefs(updated) }
        dismiss()
    }
}
