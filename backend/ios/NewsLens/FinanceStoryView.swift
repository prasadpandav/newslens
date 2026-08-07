import SwiftUI

/// The financial story reader.
///
/// It is the general reader's column of serif prose plus the three things that
/// only exist because the finance pipeline extracted them, in the order a
/// reader of a money story actually wants them:
///
///   1. the numbers, first and unmissable, each carrying the span it was read
///      from — a financial story whose figures are buried in paragraph four is
///      a general news story about a company;
///   2. who reads it which way, because a fine is a loss to the executive and
///      a win to the regulator, and one blended sentiment score hides exactly
///      the thing worth knowing;
///   3. what it connects to, taken from the knowledge graph rather than from
///      prose, so a stated relationship is a stored one.
///
/// Every section is conditional. A story with no numbers, no actor split or no
/// relationships renders the sections it has and nothing else — a thin story
/// should look thin, not look broken.
struct FinanceStoryView: View {
    @Environment(\.palette) private var pal
    @Environment(\.dismiss) private var dismiss

    let storyID: String
    @EnvironmentObject var api: APIClient
    @State private var story: FinanceStory?
    @State private var error: String?
    /// Which figure has its source span open. One at a time: the spans are a
    /// spot-check, not a second body of text to read straight through.
    @State private var openMetric: String?

    var body: some View {
        ZStack {
            InkBackground()
            VStack(spacing: 0) {
                topBar
                if let s = story {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 0) { article(s) }
                            .padding(.horizontal, 22)
                            .padding(.bottom, 48)
                    }
                    .scrollIndicators(.hidden)
                } else if let error {
                    Spacer()
                    ContentUnavailableView("Couldn't load this story",
                                           systemImage: "exclamationmark.triangle",
                                           description: Text(error))
                    Spacer()
                } else {
                    Spacer()
                    ProgressView().tint(pal.accent)
                    Spacer()
                }
            }
        }
        .toolbar(.hidden, for: .navigationBar)
        .task {
            do { story = try await api.fetchFinanceStory(id: storyID) }
            catch { self.error = "Server unreachable." }
        }
    }

    // MARK: - Bar

    private var topBar: some View {
        VStack(spacing: 0) {
            HStack {
                Button { dismiss() } label: {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 15, weight: .medium))
                        .foregroundStyle(pal.mute)
                        .frame(width: 44, height: 30, alignment: .leading)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Back")
                Spacer()
                if let s = story, let cred = s.credibility {
                    HStack(spacing: 6) {
                        PipScale(credibility: cred, width: 9, height: 4)
                        Text("\(Int(cred.rounded()))")
                            .font(pal.sans(12.5, .medium))
                            .foregroundStyle(pal.credColor(cred))
                    }
                    .accessibilityLabel(AgreementBand.sentence(cred))
                }
                Spacer()
                if let s = story {
                    ShareLink(item: api.shareURL("finance/story/\(s.id)"),
                              subject: Text(s.headline)) {
                        Image(systemName: "square.and.arrow.up")
                            .font(.system(size: 14))
                            .foregroundStyle(pal.mute)
                    }
                    .frame(width: 44, height: 30, alignment: .trailing)
                } else {
                    Color.clear.frame(width: 44, height: 30)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            Rectangle().fill(pal.hairline).frame(height: 1)
        }
    }

    // MARK: - Article

    @ViewBuilder
    private func article(_ s: FinanceStory) -> some View {
        kicker(s)
        Text(s.headline)
            .font(pal.serif(29, .light))
            .lineSpacing(4)
            .kerning(-0.4)
            .foregroundStyle(pal.text)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.bottom, 10)
        byline(s)

        if !s.keyMetrics.isEmpty { metricStrip(s) }

        if let img = s.imageUrl, !img.isEmpty {
            StoryImage(urlString: img, height: 190)
                .padding(.bottom, 18)
        }

        ForEach(Array(s.readerBeats.enumerated()), id: \.offset) { idx, beat in
            if !beat.label.isEmpty {
                sectionLabel(beat.label, tint: idx == 0 ? pal.breaking : pal.faint)
            }
            Text(beat.text)
                .font(pal.serif(17.5, .light))
                .lineSpacing(7)
                .foregroundStyle(idx == 0 ? pal.text : pal.text2)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 14)
        }

        if !s.actors.isEmpty { actorSection(s) }

        if let why = s.whyMatters, !why.isEmpty {
            sectionLabel("Why it matters")
            Text(why)
                .font(pal.serif(17, .light))
                .lineSpacing(7)
                .foregroundStyle(pal.text2)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 4)
        }

        if let rels = s.relationships, !rels.isEmpty { relationshipSection(rels) }
        if let drivers = s.economicDrivers, !drivers.isEmpty { driverSection(drivers) }
        footer(s)
    }

    /// Event type and the companies with a resolved symbol. An unresolved
    /// company is deliberately absent: the pipeline refuses to guess a ticker,
    /// and a chip is exactly the place a guess would look authoritative.
    @ViewBuilder
    private func kicker(_ s: FinanceStory) -> some View {
        HStack(spacing: 8) {
            Text(FinanceEventType.label(s.eventType))
                .font(pal.mono(11, .medium))
                .kerning(1.5)
                .textCase(.uppercase)
                .foregroundStyle(pal.breaking)
            if let tickers = s.tickers, !tickers.isEmpty {
                Circle().fill(pal.ghost).frame(width: 3, height: 3)
                ForEach(tickers.prefix(3), id: \.self) { t in
                    Text(t)
                        .font(pal.mono(11, .medium))
                        .foregroundStyle(pal.accent)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(
                            RoundedRectangle(cornerRadius: pal.r(4))
                                .fill(pal.accent.opacity(0.12)))
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.top, 18)
        .padding(.bottom, 10)
    }

    private func byline(_ s: FinanceStory) -> some View {
        HStack(spacing: 7) {
            Text("By the Descry desk")
            Circle().fill(pal.ghost).frame(width: 3, height: 3)
            Text(Date(timeIntervalSince1970: s.createdAt ?? 0)
                .formatted(.dateTime.day().month(.abbreviated).year()))
            if let n = s.sources?.count, n > 0 {
                Circle().fill(pal.ghost).frame(width: 3, height: 3)
                Text("\(n) source\(n == 1 ? "" : "s")")
            }
            Spacer(minLength: 0)
        }
        .font(pal.sans(13))
        .foregroundStyle(pal.mute)
        .padding(.bottom, 16)
    }

    // MARK: - The numbers

    /// The extracted figures, before the prose. Tapping one reveals the exact
    /// span it was read from — the pipeline drops any figure it cannot quote,
    /// so this is a promise the product can actually keep, and showing it is
    /// what makes "we did not make this number up" checkable rather than
    /// asserted.
    @ViewBuilder
    private func metricStrip(_ s: FinanceStory) -> some View {
        sectionLabel("The numbers")
        ScrollView(.horizontal) {
            HStack(spacing: 10) {
                ForEach(s.keyMetrics) { m in
                    metricCard(m)
                }
            }
            .padding(.vertical, 2)
        }
        .scrollIndicators(.hidden)
        .padding(.bottom, 10)

        if let open = openMetric,
           let m = s.keyMetrics.first(where: { $0.id == open }),
           let v = m.verbatim, !v.isEmpty {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "quote.opening")
                    .font(.system(size: 10))
                    .foregroundStyle(pal.faint)
                    .padding(.top, 3)
                Text(v)
                    .font(pal.serif(14.5, .light))
                    .lineSpacing(4)
                    .foregroundStyle(pal.text3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: pal.r(10)).fill(pal.surface2))
            .padding(.bottom, 16)
            .transition(.opacity)
        }
    }

    private func metricCard(_ m: FinanceMetric) -> some View {
        let isOpen = openMetric == m.id
        return Button {
            withAnimation(BL.spring) { openMetric = isOpen ? nil : m.id }
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                Text(m.display)
                    .font(pal.mono(19, .medium))
                    .foregroundStyle(pal.text)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                Text(m.label)
                    .font(pal.sans(11.5, .medium))
                    .foregroundStyle(pal.text3)
                    .lineLimit(1)
                HStack(spacing: 5) {
                    if let e = m.entity, !e.isEmpty {
                        Text(e)
                            .font(pal.sans(10.5))
                            .foregroundStyle(pal.faint)
                            .lineLimit(1)
                    }
                    if let p = m.period, !p.isEmpty {
                        Text(p).font(pal.mono(10)).foregroundStyle(pal.faint)
                    }
                }
                if let note = m.basisNote {
                    // Guidance is not a result. Marked, never left to the
                    // reader to infer from the label.
                    Text(note.uppercased())
                        .font(pal.mono(9, .medium))
                        .kerning(0.8)
                        .foregroundStyle(pal.warning)
                }
            }
            .padding(12)
            .frame(width: 148, alignment: .leading)
            .frame(maxHeight: .infinity, alignment: .top)
            .background(
                RoundedRectangle(cornerRadius: pal.r(12))
                    .fill(pal.surface)
                    .overlay(
                        RoundedRectangle(cornerRadius: pal.r(12))
                            .stroke(isOpen ? pal.accent.opacity(0.5) : pal.hairline,
                                    lineWidth: 1)))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(m.label), \(m.display). Tap to see the source wording.")
    }

    // MARK: - Who reads it how

    @ViewBuilder
    private func actorSection(_ s: FinanceStory) -> some View {
        sectionLabel("Who reads it how")
        Text(s.actorsDisagree
             ? "The people in this story do not want the same outcome. Each row is one side's reading, and what they are trying to protect."
             : "How each side in this story is likely to read it, and what they are trying to protect.")
            .font(pal.sans(13.5))
            .lineSpacing(4)
            .foregroundStyle(pal.text3)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.bottom, 14)
        VStack(spacing: 0) {
            ForEach(s.actors) { a in
                actorRow(a)
            }
        }
        .padding(.bottom, 6)
    }

    private func actorRow(_ a: FinanceActor) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(a.actor.capitalizedFirst)
                    .font(pal.sans(15, .medium))
                    .foregroundStyle(pal.text)
                    .fixedSize(horizontal: false, vertical: true)
                Text(a.roleLabel)
                    .font(pal.mono(10))
                    .textCase(.uppercase)
                    .kerning(0.6)
                    .foregroundStyle(pal.faint)
                Spacer(minLength: 4)
                Text(a.sentiment > 0.05 ? "in their favour"
                     : a.sentiment < -0.05 ? "against them" : "neutral")
                    .font(pal.sans(11.5))
                    .foregroundStyle(pal.text3)
            }
            SentimentBar(value: a.sentiment)
            if let w = a.wants, !w.isEmpty {
                Text(w)
                    .font(pal.sans(13.5))
                    .lineSpacing(4)
                    .foregroundStyle(pal.text3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let p = a.statedPosition, !p.isEmpty {
                // A quote only appears when the actor actually spoke. The
                // pipeline returns "" rather than paraphrasing, so an empty
                // one here means silence, not a missing field.
                Text("“\(p)”")
                    .font(pal.serif(14, .light))
                    .lineSpacing(4)
                    .foregroundStyle(pal.text2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 13)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .top) { Rectangle().fill(pal.hairline).frame(height: 1) }
    }

    // MARK: - Connections

    @ViewBuilder
    private func relationshipSection(_ rels: [FinanceRelationship]) -> some View {
        sectionLabel("What this connects")
        Text("Relationships this story established, held in the finance knowledge graph. Each one was stated in the reporting, not inferred.")
            .font(pal.sans(13.5))
            .lineSpacing(4)
            .foregroundStyle(pal.text3)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.bottom, 12)
        ForEach(rels) { r in
            HStack(spacing: 9) {
                Text(r.from)
                    .font(pal.sans(14, .medium))
                    .foregroundStyle(pal.text)
                Image(systemName: "arrow.right")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(pal.accent)
                Text(r.verb)
                    .font(pal.mono(11.5))
                    .foregroundStyle(pal.text3)
                Image(systemName: "arrow.right")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(pal.accent)
                Text(r.to)
                    .font(pal.sans(14, .medium))
                    .foregroundStyle(pal.text)
                Spacer(minLength: 0)
            }
            .padding(.vertical, 11)
            .overlay(alignment: .top) { Rectangle().fill(pal.hairline).frame(height: 1) }
        }
        .padding(.bottom, 6)
    }

    @ViewBuilder
    private func driverSection(_ drivers: [String]) -> some View {
        sectionLabel("What is driving it")
        BLFlow(spacing: 8, lineSpacing: 8) {
            ForEach(drivers, id: \.self) { d in
                Chip(text: d, color: pal.text2)
            }
        }
        .padding(.bottom, 6)
    }

    @ViewBuilder
    private func footer(_ s: FinanceStory) -> some View {
        if let srcs = s.sources, !srcs.isEmpty {
            sectionLabel("Where it came from")
            Text(srcs.joined(separator: " · "))
                .font(pal.sans(13))
                .lineSpacing(4)
                .foregroundStyle(pal.text3)
                .fixedSize(horizontal: false, vertical: true)
        }
        // Not a disclaimer bolted on at the end: the pipeline is forbidden from
        // producing advice, and saying so is part of keeping that promise
        // visible wherever the output is read.
        Text("Figures are taken from the sources above and shown with the wording they came from. This is reporting and analysis, not investment advice.")
            .font(pal.sans(13))
            .lineSpacing(4)
            .foregroundStyle(pal.faint)
            .padding(.top, 22)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func sectionLabel(_ text: String, tint: Color? = nil) -> some View {
        Text(text)
            .font(pal.mono(12, .medium))
            .kerning(1.68)
            .textCase(.uppercase)
            .foregroundStyle(tint ?? pal.faint)
            .padding(.top, 26)
            .padding(.bottom, 10)
    }
}

/// A sentiment reading as a bar that grows out from the centre, not a filled
/// track. The centre matters: these values are signed, and "0.0" and "-0.9" are
/// opposite readings rather than "empty" and "nearly full".
struct SentimentBar: View {
    @Environment(\.palette) private var pal
    let value: Double

    var body: some View {
        GeometryReader { geo in
            let half = geo.size.width / 2
            let mag = min(abs(value), 1.0)
            let w = max(mag * half, mag > 0.02 ? 2 : 0)
            ZStack(alignment: .leading) {
                Capsule().fill(pal.surface2).frame(height: 6)
                Rectangle().fill(pal.hairline2).frame(width: 1)
                    .offset(x: half - 0.5)
                Capsule()
                    .fill(value >= 0 ? pal.goodFill : pal.badFill)
                    .frame(width: w, height: 6)
                    .offset(x: value >= 0 ? half : half - w)
            }
            .frame(height: 8)
        }
        .frame(height: 8)
        .accessibilityLabel(value >= 0
                            ? "Reads as favourable, strength \(Int(abs(value) * 100)) percent"
                            : "Reads as unfavourable, strength \(Int(abs(value) * 100)) percent")
    }
}
