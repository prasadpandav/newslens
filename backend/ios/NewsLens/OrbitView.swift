import SwiftUI

// MARK: - The orbit
//
// The home's personalised filter. The reader sits at the centre; each of the
// day's stories is one word on a ring around them — `direct` when it lands on
// their work or their own details, `near` when it touches their city or what
// they follow, `wider` when nothing in the lens put it there but it is one of
// the day's biggest. Tapping a word lists its stories under the orbit; with
// nothing tapped, the feed underneath is the ordinary list.
//
// Which ring a story is on, and the one word it is drawn as, are decided by
// the server (`GET /orbit`, see backend/app/orbit.py). This file only draws.

/// The gold of the orbit mock-ups: the sand panel's edge, which is the one
/// warm accent the Descry palette already has. Signal's sandEdge is ink, so a
/// fill in it needs paper-coloured type on top — `onGold` handles that.
private extension Palette {
    var gold: Color { sandEdge }
    var onGold: Color { skin == .signal ? ink2 : .hex(0x17150F) }
}

struct OrbitCanvas: View {
    let orbit: Orbit
    @Binding var selected: String?
    /// A lens chip narrows the orbit by dimming, not removing: the spatial
    /// arrangement is the point, and nodes jumping around on a chip tap would
    /// throw it away.
    var lens: OrbitLensKind?
    var onCentre: () -> Void

    @Environment(\.palette) private var pal

    /// Ring radii as fractions of the canvas width — measured off the design
    /// (57, 106 and 155pt on a 390pt phone).
    private static let radii: [OrbitRing: CGFloat] = [.direct: 0.146, .near: 0.272, .wider: 0.397]
    /// Where each ring's first node sits, in degrees clockwise from 3 o'clock.
    /// Staggered so that nodes on neighbouring rings do not line up radially
    /// and stack their labels.
    private static let start: [OrbitRing: Double] = [.direct: -60, .near: -116, .wider: 200]
    static let height: CGFloat = 340

    var body: some View {
        GeometryReader { geo in
            let w = min(geo.size.width, 430)
            let c = CGPoint(x: geo.size.width / 2, y: geo.size.height / 2)
            let pos = positions(width: w, centre: c)
            ZStack {
                rings(width: w, centre: c)
                links(pos)
                ringLabels(width: w, centre: c)
                ForEach(orbit.nodes) { node in
                    if let p = pos[node.id] { nodeView(node, at: p) }
                }
                centre.position(c)
            }
        }
        .frame(height: Self.height)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Your orbit")
    }

    // MARK: Layout

    /// Where a node sits, and the direction from the centre to it — which is
    /// where its label goes on the crowded inner ring.
    struct Spot {
        var point: CGPoint
        var angle: Double      // radians, clockwise from 3 o'clock
    }

    private func positions(width: CGFloat, centre c: CGPoint) -> [String: Spot] {
        var out: [String: Spot] = [:]
        for ring in OrbitRing.allCases {
            let nodes = orbit.nodes.filter { $0.ring == ring }
            guard !nodes.isEmpty else { continue }
            let r = width * (Self.radii[ring] ?? 0.3)
            let step = 360.0 / Double(nodes.count)
            // Keep clear of 6 o'clock, where the ring's own label sits. The
            // clearance is an arc length (~42pt), so on the small direct ring
            // it is a much wider angle than on the outer ones.
            let clear = min(60, Double(42 / r) * 180 / .pi)
            for (i, node) in nodes.enumerated() {
                var deg = (Self.start[ring] ?? 0) + step * Double(i)
                let norm = (deg.truncatingRemainder(dividingBy: 360) + 360)
                    .truncatingRemainder(dividingBy: 360)
                if abs(norm - 90) < clear { deg += (norm < 90 ? -1 : 1) * (clear - abs(norm - 90)) }
                let rad = deg * .pi / 180
                out[node.id] = Spot(point: CGPoint(x: c.x + r * cos(rad), y: c.y + r * sin(rad)),
                                    angle: rad)
            }
        }
        return out
    }

    // MARK: Drawing

    private func rings(width w: CGFloat, centre c: CGPoint) -> some View {
        ZStack {
            ForEach([OrbitRing.wider, .near], id: \.self) { ring in
                let r = w * (Self.radii[ring] ?? 0)
                Circle()
                    .stroke(pal.hairline2, lineWidth: 1)
                    .frame(width: r * 2, height: r * 2)
                    .position(c)
            }
            let r = w * (Self.radii[.direct] ?? 0)
            Circle()
                .fill(pal.surface2)
                .overlay(Circle().stroke(pal.gold.opacity(0.45), lineWidth: 1))
                .frame(width: r * 2, height: r * 2)
                .position(c)
        }
        .allowsHitTesting(false)
    }

    private func ringLabels(width w: CGFloat, centre c: CGPoint) -> some View {
        ZStack {
            ForEach(OrbitRing.allCases, id: \.self) { ring in
                let r = w * (Self.radii[ring] ?? 0)
                Text(ring.label.uppercased())
                    .font(pal.mono(9.5, .medium))
                    .kerning(1.4)
                    .foregroundStyle(pal.faint)
                    .position(x: c.x, y: c.y + r - 11)
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    /// Dotted lines for the AI-inferred hidden links between two nodes.
    private func links(_ pos: [String: Spot]) -> some View {
        Canvas { ctx, _ in
            for l in orbit.links {
                guard let a = pos[l.a]?.point, let b = pos[l.b]?.point else { continue }
                var path = Path()
                path.move(to: a)
                path.addLine(to: b)
                let lit = lens == nil || (isInLens(l.a) && isInLens(l.b))
                ctx.stroke(path, with: .color(pal.gold.opacity(lit ? 0.6 : 0.15)),
                           style: StrokeStyle(lineWidth: 1.2, lineCap: .round, dash: [1.5, 4.5]))
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private var centre: some View {
        Button(action: onCentre) {
            Text(orbit.lens.set ? "You" : "Set lens")
                .font(orbit.lens.set ? pal.serif(17).italic() : pal.sans(11, .medium))
                .foregroundStyle(pal.onGold)
                .frame(width: 46, height: 46)
                .background(Circle().fill(pal.gold))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(orbit.lens.set ? "You — show all stories" : "Set your lens")
    }

    private func isInLens(_ nodeID: String) -> Bool {
        guard let lens else { return true }
        return orbit.nodes.first { $0.id == nodeID }?.lenses.contains(lens.rawValue) ?? false
    }

    /// A node is two tap targets with the same action: the dot, which carries
    /// the accessibility label, and its word. They are placed separately
    /// because on the direct ring the word goes OUTWARD from the centre —
    /// placed underneath, four words on a 57pt ring run into "You" and into
    /// each other.
    @ViewBuilder
    private func nodeView(_ node: OrbitNode, at spot: Spot) -> some View {
        let on = selected == node.id
        let lit = isInLens(node.id)
        let d: CGFloat = node.ring == .direct ? 20 : 14
        let p = spot.point
        let tap = { withAnimation(BL.spring) { selected = on ? nil : node.id } }
        let place = labelPlacement(node.ring, spot: spot, dot: d)
        Group {
            Button(action: tap) {
                dot(node.ring, on: on)
                    .frame(width: d, height: d)
                    .background {
                        if on {
                            Circle().stroke(pal.text.opacity(0.8), lineWidth: 1.5)
                                .frame(width: d + 10, height: d + 10)
                        }
                    }
                    .frame(width: 40, height: 40)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .position(p)
            .accessibilityLabel("\(node.word), \(node.ring.label), "
                                + "\(node.storyIDs.count) \(node.storyIDs.count == 1 ? "story" : "stories")")
            .accessibilityAddTraits(on ? [.isSelected] : [])
            Button(action: tap) {
                Text(node.word)
                    .font(pal.sans(12.5, on ? .semibold : .regular))
                    .foregroundStyle(on ? pal.text : pal.text2)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                    .frame(width: 84, height: 22, alignment: place.alignment)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            // The dot already speaks for the node; the word would be a
            // second, duplicate stop for VoiceOver.
            .accessibilityHidden(true)
            .position(place.centre)
        }
        .opacity(lit ? 1 : 0.22)
        .disabled(!lit)
    }

    /// Below the dot on the outer rings; on the direct ring, on whichever side
    /// faces away from the centre.
    private func labelPlacement(_ ring: OrbitRing, spot: Spot, dot d: CGFloat)
        -> (centre: CGPoint, alignment: Alignment) {
        let p = spot.point
        let below = (CGPoint(x: p.x, y: p.y + d / 2 + 12), Alignment.center)
        guard ring == .direct else { return below }
        let cx = cos(spot.angle), cy = sin(spot.angle)
        if abs(cx) > 0.45 {
            // 84pt frame, text pinned to the edge nearest the dot.
            // Clear of the selection halo (5pt beyond the dot) as well.
            let off = d / 2 + 10 + 42
            return cx > 0 ? (CGPoint(x: p.x + off, y: p.y), .leading)
                          : (CGPoint(x: p.x - off, y: p.y), .trailing)
        }
        return cy < 0 ? (CGPoint(x: p.x, y: p.y - d / 2 - 12), .center) : below
    }

    @ViewBuilder
    private func dot(_ ring: OrbitRing, on: Bool) -> some View {
        switch ring {
        case .direct:
            Circle().fill(pal.gold)
        case .near:
            Circle().fill(on ? pal.text : pal.text.opacity(0.85))
        case .wider:
            Circle().fill(on ? pal.text : pal.ink)
                .overlay(Circle().stroke(pal.text2, lineWidth: 1.5))
        }
    }
}

// MARK: - Lens chips

/// "All 7 · Work 2 · Money 3 · City 1 · Family 2". Counts are stories, and a
/// story can sit under more than one lens — so the parts need not sum to All.
struct OrbitLensChips: View {
    let orbit: Orbit
    @Binding var lens: OrbitLensKind?
    @Environment(\.palette) private var pal

    private func count(_ l: OrbitLensKind?) -> Int {
        guard let l else { return orbit.stories.count }
        return orbit.stories.values.filter { $0.lenses.contains(l.rawValue) }.count
    }

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 7) {
                chip(nil)
                ForEach(OrbitLensKind.allCases) { l in
                    if count(l) > 0 { chip(l) }
                }
            }
            // Bleed by widening the scroll view, never with scrollClipDisabled —
            // chips drawn outside the scroll view's frame are dead to taps.
            .padding(.horizontal, 20)
            .padding(.vertical, 2)
        }
        .padding(.horizontal, -20)
    }

    private func chip(_ l: OrbitLensKind?) -> some View {
        let on = lens == l
        return Button {
            withAnimation(BL.spring) { lens = l }
        } label: {
            HStack(spacing: 6) {
                Text(l?.label ?? "All").font(pal.sans(14, on ? .medium : .regular))
                Text("\(count(l))").font(pal.mono(11.5)).opacity(0.6)
            }
            .foregroundStyle(on ? pal.ink : pal.text2)
            .padding(.horizontal, 13).padding(.vertical, 8)
            .background(shape.fill(on ? pal.text : pal.surface2))
            .contentShape(shape)
        }
        .buttonStyle(.plain)
        // Directly on the Button: wrapping it with accessibilityElement(children:)
        // turns the chip into a plain element and VoiceOver loses the button.
        .accessibilityLabel(l?.label ?? "All")
        .accessibilityValue("\(count(l)) \(count(l) == 1 ? "story" : "stories")")
        .accessibilityAddTraits(on ? [.isSelected] : [])
    }

    private var shape: AnyShape {
        pal.radius == 0 ? AnyShape(Rectangle()) : AnyShape(Capsule())
    }
}

// MARK: - The panel under a tapped node

/// Pushed when "Why me?" is tapped. Hashable so it can ride a NavigationLink.
struct WhyRoute: Hashable {
    let item: FeedItem
    let info: OrbitStory
}

/// The stories on one node, drawn as the design's bottom sheet: a handle, then
/// one card per story. Sits in the scroll column rather than being a real
/// sheet, so the orbit above it stays tappable and the tab bar stays put.
struct OrbitPanel: View {
    let node: OrbitNode
    let items: [FeedItem]
    let orbit: Orbit
    /// Personal angles fetched this session, by story id.
    let impacts: [String: String]
    var onClose: () -> Void
    @Environment(\.palette) private var pal

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Capsule().fill(pal.hairline2)
                .frame(width: 36, height: 4)
                .frame(maxWidth: .infinity)
                .padding(.top, 10).padding(.bottom, 14)
            if items.count > 1 {
                HStack {
                    Text("\(items.count) stories on \(node.word)")
                        .font(pal.mono(11, .medium)).kerning(1.4).textCase(.uppercase)
                        .foregroundStyle(pal.faint)
                    Spacer()
                }
                .padding(.bottom, 12)
            }
            ForEach(Array(items.enumerated()), id: \.element.id) { i, item in
                if i > 0 {
                    Rectangle().fill(pal.hairline).frame(height: 1).padding(.vertical, 16)
                }
                if let info = orbit.stories[item.id] {
                    OrbitStoryCard(item: item, info: info,
                                   impact: impacts[item.id] ?? item.impactText)
                }
            }
            Button(action: onClose) {
                HStack(spacing: 6) {
                    Image(systemName: "list.bullet").font(.system(size: 11, weight: .medium))
                    Text("All of today's stories").font(pal.sans(13.5))
                }
                .foregroundStyle(pal.text2)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.top, 14)
        }
        .padding(.horizontal, 20)
        .padding(.bottom, 8)
        .background(
            UnevenRoundedRectangle(topLeadingRadius: pal.r(22), topTrailingRadius: pal.r(22),
                                   style: .continuous)
                .fill(pal.ink2)
                .overlay(UnevenRoundedRectangle(topLeadingRadius: pal.r(22),
                                                topTrailingRadius: pal.r(22), style: .continuous)
                    .stroke(pal.hairline2, lineWidth: 1))
        )
        .padding(.horizontal, -20)   // edge to edge, like the sheet it stands for
    }
}

/// One story in the panel: kicker, headline, the personal line, whether a
/// hidden link was found, then "Why me?" and "Read".
struct OrbitStoryCard: View {
    let item: FeedItem
    let info: OrbitStory
    var impact: String?
    @Environment(\.palette) private var pal

    private var kicker: String {
        [info.lensLabel ?? item.sectionLabel, info.ring.label].joined(separator: " · ")
    }

    /// The reader's personal angle when one has been worked out; otherwise
    /// their own words quoted back; otherwise an honest "outside your lens".
    private var forYou: String {
        if let impact, !impact.isEmpty { return impact }
        if let e = info.exposure { return e.text }
        return "Outside your lens — here because it is one of today's most-covered stories."
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(kicker)
                    .font(pal.mono(11.5, .medium)).kerning(1.5).textCase(.uppercase)
                    .foregroundStyle(pal.gold)
                    .lineLimit(1)
                Spacer(minLength: 8)
                AgreementLine(credibility: item.credibility, size: 11.5, pipWidth: 8)
                    .layoutPriority(-1)
            }
            .padding(.bottom, 10)
            Text(item.headline)
                .font(pal.serif(23))
                .lineSpacing(2)
                .foregroundStyle(pal.text)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 12)
            (Text("For you — ").font(pal.serif(16).italic()).foregroundColor(pal.gold)
             + Text(forYou).font(pal.sans(15)).foregroundColor(pal.text2))
                .lineSpacing(4)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 12)
            HiddenLinkLine(links: info.hiddenLinks)
                .padding(.bottom, 16)
            HStack(spacing: 10) {
                Text("\(item.readingMinutes) min · AI-written")
                    .font(pal.mono(11, .medium)).kerning(1.2).textCase(.uppercase)
                    .foregroundStyle(pal.faint)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                Spacer(minLength: 4)
                NavigationLink(value: WhyRoute(item: item, info: info)) {
                    Text("Why me?")
                        .font(pal.sans(15, .medium))
                        .foregroundStyle(pal.text)
                        .padding(.horizontal, 18).padding(.vertical, 11)
                        .overlay(pill.stroke(pal.hairline2, lineWidth: 1))
                        .contentShape(pill)
                }
                .buttonStyle(.plain)
                NavigationLink(value: item) {
                    Text("Read")
                        .font(pal.sans(15, .medium))
                        .foregroundStyle(pal.ink)
                        .padding(.horizontal, 22).padding(.vertical, 11)
                        .background(pill.fill(pal.text))
                        .contentShape(pill)
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var pill: AnyShape {
        pal.radius == 0 ? AnyShape(Rectangle()) : AnyShape(Capsule())
    }
}

/// "···· HIDDEN LINK TO FREIGHT" or "···· NO HIDDEN LINKS FOUND TODAY".
struct HiddenLinkLine: View {
    let links: [OrbitStory.HiddenLink]
    @Environment(\.palette) private var pal

    var body: some View {
        HStack(spacing: 8) {
            Text("····").font(pal.mono(11, .bold)).foregroundStyle(pal.gold.opacity(0.8))
            Text(text)
                .font(pal.mono(10.5, .medium)).kerning(1.3).textCase(.uppercase)
                .foregroundStyle(links.isEmpty ? pal.faint : pal.text2)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        }
    }

    private var text: String {
        guard let first = links.first else { return "No hidden links found today" }
        let to = first.word ?? "another story"
        return links.count == 1 ? "Hidden link to \(to) · AI-inferred"
                                : "\(links.count) hidden links · AI-inferred"
    }
}

// MARK: - Why this reaches you

struct WhyMeView: View {
    let route: WhyRoute
    /// The personal angle, if the home already has one for this story.
    var impact: String?
    /// Hands a freshly worked-out angle back to the home, so the card under
    /// the orbit shows it too.
    var onImpact: (String) -> Void = { _ in }
    var onNotRelevant: () -> Void = {}
    var onLensChanged: () -> Void = {}

    @EnvironmentObject private var api: APIClient
    @Environment(\.palette) private var pal
    @Environment(\.dismiss) private var dismiss
    @State private var meaning: String?
    @State private var working = false
    @State private var askText = ""
    @State private var asking: String?
    @State private var editingLens = false

    private var item: FeedItem { route.item }
    private var info: OrbitStory { route.info }

    var body: some View {
        ZStack {
            InkBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    Text([info.lensLabel ?? item.sectionLabel, info.ring.kicker]
                        .joined(separator: " · "))
                        .font(pal.mono(11.5, .medium)).kerning(1.5).textCase(.uppercase)
                        .foregroundStyle(pal.gold)
                        .padding(.bottom, 8)
                    Text(item.headline)
                        .font(pal.serif(28))
                        .lineSpacing(1)
                        .foregroundStyle(pal.text)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.bottom, 10)
                    HStack(spacing: 8) {
                        AgreementLine(credibility: item.credibility, size: 12.5, pipWidth: 9)
                        if let facts = item.factsCheckedLine {
                            Text("· \(facts)").font(pal.mono(12)).foregroundStyle(pal.mute)
                                .lineLimit(1)
                        }
                    }
                    .padding(.bottom, 24)
                    sectionHead("The path to you")
                    path.padding(.top, 16)
                    lensUsed.padding(.top, 26)
                }
                .padding(.horizontal, 20)
                .padding(.top, 8)
                .padding(.bottom, 24)
            }
            .scrollIndicators(.hidden)
        }
        .safeAreaInset(edge: .bottom) { askBar }
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            // A caption, not a control — so no Liquid Glass capsule behind it.
            if #available(iOS 26.0, *) {
                ToolbarItem(placement: .topBarTrailing) { caption }
                    .sharedBackgroundVisibility(.hidden)
            } else {
                ToolbarItem(placement: .topBarTrailing) { caption }
            }
        }
        .sheet(item: Binding(get: { asking.map(AskRequest.init) },
                             set: { asking = $0?.question })) { req in
            AskAISheet(story: nil, storyID: item.id, storyHeadline: item.headline,
                       firstQuestion: req.question)
                .environmentObject(api).skinned()
        }
        .sheet(isPresented: $editingLens) {
            OnboardingView(initial: UserContext.saved) {
                editingLens = false
                onLensChanged()
            }
            .environmentObject(api).skinned()
        }
        .task { await workOutMeaning() }
    }

    private var caption: some View {
        Text("Why this reaches you")
            .font(pal.mono(11, .medium)).kerning(1.4).textCase(.uppercase)
            .foregroundStyle(pal.faint)
    }

    private struct AskRequest: Identifiable {
        let question: String
        var id: String { question }
    }

    // MARK: The path

    private var path: some View {
        VStack(alignment: .leading, spacing: 0) {
            PathStep(marker: .hollow, label: "The event", last: false) {
                Text(eventLine)
                    .font(pal.sans(15.5)).lineSpacing(4).foregroundStyle(pal.text)
                    .fixedSize(horizontal: false, vertical: true)
                if let meta = eventMeta {
                    Text(meta).font(pal.mono(11, .medium)).kerning(1.2)
                        .textCase(.uppercase).foregroundStyle(pal.faint)
                        .padding(.top, 6)
                }
            }
            PathStep(marker: .filled, label: "Your exposure", last: false) {
                if let e = info.exposure {
                    Text(e.text)
                        .font(pal.sans(15.5)).lineSpacing(4).foregroundStyle(pal.text)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(e.label)
                        .font(pal.mono(10.5, .medium)).kerning(1.2).textCase(.uppercase)
                        .foregroundStyle(pal.text2)
                        .padding(.horizontal, 9).padding(.vertical, 6)
                        .overlay(RoundedRectangle(cornerRadius: pal.r(4))
                            .stroke(pal.hairline2, lineWidth: 1))
                        .padding(.top, 8)
                } else {
                    Text("Nothing in your lens touches this one directly. It is in your orbit because it is one of today's most-covered stories.")
                        .font(pal.sans(15.5)).lineSpacing(4).foregroundStyle(pal.text2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let link = info.hiddenLinks.first, !link.chain.isEmpty {
                PathStep(marker: .dashed, label: "Hidden link", last: false,
                         badge: "AI-inferred · \(link.confidenceLabel) confidence") {
                    Text(link.chain)
                        .font(pal.sans(15.5)).lineSpacing(4).foregroundStyle(pal.text)
                        .lineLimit(6)
                        .fixedSize(horizontal: false, vertical: true)
                    if let sid = link.storyID,
                       let other = otherItem(sid) {
                        NavigationLink(value: other) {
                            Text("Open the \((link.word ?? "linked").lowercased()) story →")
                                .font(pal.sans(14.5, .medium))
                                .foregroundStyle(pal.gold)
                        }
                        .buttonStyle(.plain)
                        .padding(.top, 8)
                    } else if !link.title.isEmpty {
                        Text("Linked: \(link.title)")
                            .font(pal.sans(13.5)).foregroundStyle(pal.mute)
                            .lineLimit(2).padding(.top, 6)
                    }
                }
            }
            PathStep(marker: .gold, label: "What it means for you", last: true) {
                meaningView
            }
        }
    }

    @ViewBuilder
    private var meaningView: some View {
        let text = meaning ?? impact ?? ""
        if !text.isEmpty {
            Text(text)
                .font(pal.serif(20).italic())
                .lineSpacing(3)
                .foregroundStyle(pal.text)
                .fixedSize(horizontal: false, vertical: true)
        } else if working {
            HStack(spacing: 8) {
                ProgressView().tint(pal.gold)
                Text("Working out what it means for you…")
                    .font(pal.sans(14.5)).foregroundStyle(pal.mute)
            }
        } else if api.userID == nil {
            Text("Tell Descry your world and this is where it says what the story means for you.")
                .font(pal.sans(15)).foregroundStyle(pal.text2)
                .fixedSize(horizontal: false, vertical: true)
        } else {
            Text(info.ring == .wider
                 ? "This one sits outside your lens — nothing in it lands on you directly."
                 : "Nothing specific for you yet: the link to your lens is loose.")
                .font(pal.serif(18).italic()).foregroundStyle(pal.text2)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: Lens used

    private var lensUsed: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Lens used")
                    .font(pal.mono(11, .medium)).kerning(1.4).textCase(.uppercase)
                    .foregroundStyle(pal.faint)
                Spacer()
                Button {
                    Task { await api.sendFeedback(storyID: item.id, action: "not_relevant") }
                    onNotRelevant()
                    dismiss()
                } label: {
                    Text("Not relevant to me")
                        .font(pal.sans(14)).underline()
                        .foregroundStyle(pal.text2)
                }
                .buttonStyle(.plain)
            }
            BLFlow(spacing: 8) {
                ForEach(info.lensUsed, id: \.id) { l in
                    Text(l.label)
                        .font(pal.sans(14))
                        .foregroundStyle(pal.text)
                        .padding(.horizontal, 13).padding(.vertical, 8)
                        .background(Capsule().fill(pal.surface2))
                }
                if info.lensUsed.isEmpty {
                    Text("None — outside your lens")
                        .font(pal.sans(14)).foregroundStyle(pal.mute)
                        .padding(.vertical, 8)
                }
                Button { editingLens = true } label: {
                    Text("Edit lens")
                        .font(pal.sans(14, .medium))
                        .foregroundStyle(pal.gold)
                        .padding(.horizontal, 6).padding(.vertical, 8)
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: Ask

    private var askBar: some View {
        HStack(spacing: 8) {
            TextField("Ask what this means for you…", text: $askText)
                .textFieldStyle(.plain)
                .font(pal.sans(15))
                .foregroundStyle(pal.text)
                .submitLabel(.send)
                .onSubmit(ask)
            Button(action: ask) {
                Image(systemName: "arrow.up")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(pal.onGold)
                    .frame(width: 40, height: 40)
                    .background(Circle().fill(pal.gold))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Ask")
        }
        .padding(.leading, 18).padding(.trailing, 6).padding(.vertical, 6)
        .background(Capsule().fill(pal.ink2))
        .overlay(Capsule().stroke(pal.hairline2, lineWidth: 1))
        .padding(.horizontal, 16)
        .padding(.bottom, 8)
        .padding(.top, 6)
        .background(pal.ink.opacity(0.94))
    }

    private func ask() {
        let q = askText.trimmingCharacters(in: .whitespacesAndNewlines)
        asking = q.isEmpty ? "What does this mean for me?" : q
        askText = ""
    }

    // MARK: Data

    /// The event in one sentence: the storyline's first sentence, which the
    /// Storyteller is told to open with the event itself.
    private var eventLine: String {
        let body = item.narrative.trimmingCharacters(in: .whitespacesAndNewlines)
        var first = body
        if let r = body.range(of: #"(?<=[.!?])\s+(?=[A-Z“"])"#, options: .regularExpression) {
            first = String(body[..<r.lowerBound])
        }
        return first.count > 260 ? String(first.prefix(257)) + "…" : first
    }

    /// "5 OUTLETS · FIRST REPORTED 21:40".
    private var eventMeta: String? {
        var parts: [String] = []
        if let n = item.sourceCount, n > 0 { parts.append("\(n) outlet\(n == 1 ? "" : "s")") }
        if let at = item.createdAt, at > 0 {
            let d = Date(timeIntervalSince1970: at)
            let when = Calendar.current.isDateInToday(d)
                ? d.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute(.twoDigits))
                : d.formatted(.dateTime.day().month(.abbreviated).hour().minute())
            parts.append("First reported \(when)")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private func otherItem(_ id: String) -> FeedItem? {
        OrbitHomeCache.shared.items.first { $0.id == id }
    }

    /// Ask the personalizer once, only for stories the lens actually reaches
    /// and only when no angle is cached yet. The server caches per reader and
    /// story, so reopening this screen costs nothing.
    private func workOutMeaning() async {
        guard meaning == nil, (impact ?? "").isEmpty, info.ring != .wider,
              api.userID != nil, !working else { return }
        working = true
        if let r = await api.personalizeStory(storyID: item.id) {
            meaning = r.text
            if !r.text.isEmpty { onImpact(r.text) }
        }
        working = false
    }

    private func sectionHead(_ title: String) -> some View {
        HStack(spacing: 12) {
            Text(title)
                .font(pal.mono(11, .medium)).kerning(1.4).textCase(.uppercase)
                .foregroundStyle(pal.faint)
                .fixedSize()
            Rectangle().fill(pal.hairline).frame(height: 1)
        }
    }
}

/// The items behind the current orbit, so the Why-me screen can open the
/// story on the far side of a hidden link without the route carrying the
/// whole feed. Written by the home on every load.
@MainActor
final class OrbitHomeCache {
    static let shared = OrbitHomeCache()
    var items: [FeedItem] = []
}

/// One step on "The path to you": a marker on a vertical rule, a label, then
/// whatever the step says.
private struct PathStep<Content: View>: View {
    enum Marker { case hollow, filled, dashed, gold }
    let marker: Marker
    let label: String
    let last: Bool
    var badge: String? = nil
    @ViewBuilder var content: Content
    @Environment(\.palette) private var pal

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(spacing: 0) {
                markerView.frame(width: 14, height: 14).padding(.top, 1)
                if !last {
                    Rectangle().fill(pal.hairline2).frame(width: 1)
                        .frame(maxHeight: .infinity)
                        .padding(.top, 4)
                }
            }
            .frame(width: 16)
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    Text(label)
                        .font(pal.mono(11, .medium)).kerning(1.4).textCase(.uppercase)
                        .foregroundStyle(marker == .gold ? pal.gold : pal.faint)
                    if let badge {
                        Text(badge)
                            .font(pal.mono(10, .medium)).kerning(1).textCase(.uppercase)
                            .foregroundStyle(pal.gold)
                            .padding(.horizontal, 6).padding(.vertical, 3)
                            .overlay(RoundedRectangle(cornerRadius: pal.r(3))
                                .stroke(pal.gold.opacity(0.7),
                                        style: StrokeStyle(lineWidth: 1, dash: [2, 2])))
                            .lineLimit(1)
                            .minimumScaleFactor(0.8)
                    }
                }
                content
            }
            .padding(.bottom, last ? 0 : 22)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder
    private var markerView: some View {
        switch marker {
        case .hollow:
            Circle().stroke(pal.text2, lineWidth: 1.5).padding(1)
        case .filled:
            Circle().fill(pal.text).padding(2)
        case .dashed:
            Circle().stroke(pal.gold, style: StrokeStyle(lineWidth: 1.5, dash: [2, 2])).padding(1)
        case .gold:
            Circle().fill(pal.gold)
        }
    }
}

// MARK: - Helpers

extension UserContext {
    /// The context saved on this device by the last onboarding/lens edit.
    static var saved: UserContext? {
        guard let d = UserDefaults.standard.data(forKey: "saved_context") else { return nil }
        return try? JSONDecoder().decode(UserContext.self, from: d)
    }
}

/// Spelled-out small numbers, the way the design's summary line reads:
/// "Seven stories reach you today. Two land directly."
enum SpelledCount {
    static func of(_ n: Int) -> String {
        let words = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven",
                     "Eight", "Nine", "Ten", "Eleven", "Twelve"]
        return n >= 0 && n < words.count ? words[n] : "\(n)"
    }
}
