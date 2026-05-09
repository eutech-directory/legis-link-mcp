import {
  AbsoluteFill, useCurrentFrame, useVideoConfig,
  interpolate, spring, Sequence, Audio, staticFile
} from "remotion";

// ── Colours ────────────────────────────────────────────────────────────────
const C = {
  bg:      "#0a0f1a",
  surf:    "#111827",
  surf2:   "#1a2235",
  border:  "#1e293b",
  acc:     "#3b82f6",
  acc2:    "#06b6d4",
  green:   "#10b981",
  amber:   "#f59e0b",
  red:     "#ef4444",
  text:    "#f1f5f9",
  muted:   "#64748b",
  muted2:  "#94a3b8",
};

// ── Helpers ────────────────────────────────────────────────────────────────
const fade = (frame: number, from: number, to: number) =>
  interpolate(frame, [from, to], [0, 1], { extrapolateRight: "clamp", extrapolateLeft: "clamp" });

const slideUp = (frame: number, from: number, to: number, dist = 40) =>
  interpolate(frame, [from, to], [dist, 0], { extrapolateRight: "clamp", extrapolateLeft: "clamp" });

// ── Subtitle bar ───────────────────────────────────────────────────────────
const Sub = ({ frame, start, end, text }: { frame: number; start: number; end: number; text: string }) => (
  <div style={{
    position: "absolute", bottom: 54, left: 0, right: 0,
    display: "flex", justifyContent: "center",
    opacity: fade(frame, start, start + 8) * (1 - fade(frame, end - 8, end)),
  }}>
    <div style={{
      background: "rgba(0,0,0,0.75)", backdropFilter: "blur(6px)",
      borderRadius: 8, padding: "10px 24px",
      fontSize: 28, fontWeight: 500, color: "white",
      fontFamily: "system-ui, sans-serif", textAlign: "center", maxWidth: 900,
    }}>{text}</div>
  </div>
);

// ── Tool pill ──────────────────────────────────────────────────────────────
const Tool = ({ label, pro, delay, frame }: { label: string; pro: boolean; delay: number; frame: number }) => {
  const s = spring({ frame: frame - delay, fps: 30, config: { damping: 14, stiffness: 100 } });
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 6,
      padding: "8px 16px", borderRadius: 8, border: `1px solid ${pro ? "rgba(6,182,212,0.35)" : "rgba(59,130,246,0.35)"}`,
      background: pro ? "rgba(6,182,212,0.08)" : "rgba(59,130,246,0.08)",
      opacity: Math.min(s, 1), transform: `scale(${Math.min(s, 1)})`,
      whiteSpace: "nowrap",
    }}>
      <div style={{ width: 7, height: 7, borderRadius: "50%", background: pro ? C.acc2 : C.acc }} />
      <span style={{ fontSize: 20, color: pro ? C.acc2 : C.acc, fontFamily: "system-ui,sans-serif", fontWeight: 500 }}>{label}</span>
      {pro && <span style={{ fontSize: 13, background: "rgba(6,182,212,0.2)", color: C.acc2, padding: "1px 6px", borderRadius: 4, fontWeight: 700 }}>PRO</span>}
    </div>
  );
};

// ── Region badge ───────────────────────────────────────────────────────────
const Region = ({ flag, name, std, delay, frame }: any) => {
  const o = fade(frame, delay, delay + 12);
  const y = slideUp(frame, delay, delay + 12, 30);
  return (
    <div style={{
      background: C.surf, border: `1px solid ${C.border}`, borderRadius: 12,
      padding: "18px 24px", opacity: o, transform: `translateY(${y}px)`,
    }}>
      <div style={{ fontSize: 32, marginBottom: 6 }}>{flag}</div>
      <div style={{ fontSize: 20, fontWeight: 600, color: C.text, fontFamily: "system-ui,sans-serif" }}>{name}</div>
      <div style={{ fontSize: 14, color: C.acc2, fontFamily: "monospace", marginTop: 4 }}>{std}</div>
    </div>
  );
};

// ── Main composition ───────────────────────────────────────────────────────
export const LegisLinkProDemo = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Scene timings (at 30fps)
  // Scene 1: 0-89    — Hook / pain point
  // Scene 2: 90-209  — Live compliance answer
  // Scene 3: 210-299 — 9 Tools
  // Scene 4: 300-389 — 87 regions
  // Scene 5: 390-449 — Pricing + CTA

  return (
    <AbsoluteFill style={{ background: C.bg, fontFamily: "system-ui, sans-serif", overflow: "hidden" }}>

      {/* Background grid always on */}
      <AbsoluteFill style={{
        backgroundImage: `linear-gradient(${C.border}44 1px, transparent 1px), linear-gradient(90deg, ${C.border}44 1px, transparent 1px)`,
        backgroundSize: "80px 80px",
      }} />

      {/* ── SCENE 1: Hook (0-89) ─────────────────────────────────────────── */}
      <Sequence from={0} durationInFrames={90}>
        <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", flexDirection: "column", gap: 24 }}>

          {/* Glow */}
          <div style={{ position: "absolute", inset: 0, background: "radial-gradient(ellipse 70% 50% at 50% 50%, rgba(239,68,68,0.08) 0%, transparent 70%)" }} />

          {/* Pain statement */}
          <div style={{ opacity: fade(frame, 0, 20), transform: `translateY(${slideUp(frame, 0, 20)})`, textAlign: "center", padding: "0 80px" }}>
            <div style={{ fontSize: 20, color: C.muted2, fontFamily: "monospace", letterSpacing: "0.1em", marginBottom: 16 }}>
              THE PROBLEM
            </div>
            <div style={{ fontSize: 52, fontWeight: 700, color: C.text, lineHeight: 1.2, marginBottom: 20 }}>
              Wrong cable size.<br/>
              <span style={{ color: C.red }}>Full rework.</span>
            </div>
            <div style={{ fontSize: 26, color: C.muted2, lineHeight: 1.5 }}>
              You needed the answer at 7am.<br/>The standard was back at the office.
            </div>
          </div>

          {/* Clock icon */}
          <div style={{ opacity: fade(frame, 30, 50), fontSize: 64 }}>⏱️</div>

          {/* Solution tease */}
          <div style={{ opacity: fade(frame, 60, 80), transform: `translateY(${slideUp(frame, 60, 80)})`, textAlign: "center" }}>
            <div style={{ fontSize: 28, color: C.acc, fontWeight: 600 }}>
              There is a better way.
            </div>
          </div>
        </AbsoluteFill>

        {/* Subtitles */}
        <Sub frame={frame} start={0} end={55} text="Wrong cable size on a 30m run means a full rework." />
        <Sub frame={frame} start={55} end={90} text="You needed that answer before you pulled the cable." />
      </Sequence>

      {/* ── SCENE 2: Live answer (90-209) ───────────────────────────────── */}
      <Sequence from={90} durationInFrames={120}>
        <AbsoluteFill style={{ flexDirection: "column", padding: "40px 60px", gap: 20 }}>

          {/* Header */}
          <div style={{ opacity: fade(frame, 0, 20), display: "flex", alignItems: "center", gap: 16, marginBottom: 8 }}>
            <div style={{ width: 54, height: 54, borderRadius: 14, background: "linear-gradient(135deg,#3b82f6,#06b6d4)", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "monospace", fontSize: 22, fontWeight: 700, color: "white" }}>LL</div>
            <div>
              <div style={{ fontFamily: "monospace", fontSize: 22, fontWeight: 700, color: C.acc }}>legis-link</div>
              <div style={{ fontSize: 14, color: C.muted2 }}>Construction Compliance AI</div>
            </div>
            <div style={{ marginLeft: "auto", background: C.surf, border: `1px solid ${C.border}`, borderRadius: 8, padding: "6px 14px", fontFamily: "monospace", fontSize: 14, color: C.muted2 }}>
              AU · NSW · Electrical
            </div>
          </div>

          {/* User question bubble */}
          <div style={{ opacity: fade(frame, 15, 35), transform: `translateY(${slideUp(frame, 15, 35)})`, display: "flex", justifyContent: "flex-end" }}>
            <div style={{ background: "linear-gradient(135deg,#3b82f6,#2563eb)", borderRadius: "12px 12px 3px 12px", padding: "16px 24px", maxWidth: "70%", fontSize: 22, color: "white", lineHeight: 1.5 }}>
              Wire size for 45A load, 30m run, 240V single phase?
            </div>
          </div>

          {/* Typing indicator */}
          <Sequence from={35} durationInFrames={25}>
            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <div style={{ width: 36, height: 36, borderRadius: 10, background: C.surf2, border: `1px solid ${C.border}`, display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "monospace", fontSize: 14, color: C.acc2 }}>LL</div>
              <div style={{ background: C.surf2, border: `1px solid ${C.border}`, borderRadius: "12px 12px 12px 3px", padding: "16px 24px", display: "flex", gap: 8, alignItems: "center" }}>
                {[0, 1, 2].map(i => (
                  <div key={i} style={{ width: 10, height: 10, borderRadius: "50%", background: C.muted, opacity: interpolate(((frame - 35 - i * 6) % 18), [0, 9, 18], [0.3, 1, 0.3]) }} />
                ))}
              </div>
            </div>
          </Sequence>

          {/* Answer bubble */}
          <Sequence from={60} durationInFrames={60}>
            <div style={{ opacity: fade(frame, 0, 20), transform: `translateY(${slideUp(frame, 0, 20)})`, display: "flex", gap: 10, alignItems: "flex-start" }}>
              <div style={{ width: 36, height: 36, borderRadius: 10, background: C.surf2, border: `1px solid ${C.border}`, display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "monospace", fontSize: 14, color: C.acc2, flexShrink: 0 }}>LL</div>
              <div style={{ background: C.surf2, border: `1px solid ${C.border}`, borderRadius: "12px 12px 12px 3px", padding: "20px 28px", maxWidth: "80%" }}>
                <div style={{ fontSize: 48, fontWeight: 700, color: C.acc, fontFamily: "monospace", marginBottom: 4 }}>10mm²</div>
                <div style={{ fontSize: 20, color: C.text, marginBottom: 8 }}>copper conductor required</div>
                <div style={{ fontSize: 16, color: C.muted2, marginBottom: 16 }}>Voltage drop: 1.97% — within 3% sub-circuit limit. Clipped direct or in conduit for 45A continuous load.</div>

                {/* Badge */}
                <div style={{ opacity: fade(frame, 25, 40) }}>
                  <div style={{ display: "inline-flex", alignItems: "center", gap: 8, background: "rgba(16,185,129,0.12)", border: "1px solid rgba(16,185,129,0.3)", borderRadius: 8, padding: "6px 16px", marginBottom: 10 }}>
                    <div style={{ width: 8, height: 8, borderRadius: "50%", background: C.green }} />
                    <span style={{ fontFamily: "monospace", fontSize: 16, color: C.green, fontWeight: 700 }}>COMPLIANT</span>
                  </div>
                  <div style={{ fontFamily: "monospace", fontSize: 13, color: C.muted }}>Ref: AS/NZS 3008.1.1:2017 Table C1 · AS/NZS 3000:2018 Cl.3.6.2</div>
                </div>
              </div>
            </div>
          </Sequence>

          {/* Speed callout */}
          <Sequence from={95} durationInFrames={25}>
            <div style={{ opacity: fade(frame, 0, 15), position: "absolute", top: 40, right: 60, background: "rgba(16,185,129,0.12)", border: "1px solid rgba(16,185,129,0.3)", borderRadius: 10, padding: "10px 20px", textAlign: "center" }}>
              <div style={{ fontSize: 32, fontWeight: 700, color: C.green, fontFamily: "monospace" }}>&lt;5s</div>
              <div style={{ fontSize: 14, color: C.muted2 }}>response time</div>
            </div>
          </Sequence>

          <Sub frame={frame} start={0} end={60} text="Ask any compliance question in plain English." />
          <Sub frame={frame} start={60} end={120} text="Get the exact standard and clause number — in under 5 seconds." />
        </AbsoluteFill>
      </Sequence>

      {/* ── SCENE 3: 9 Tools (210-299) ──────────────────────────────────── */}
      <Sequence from={210} durationInFrames={90}>
        <AbsoluteFill style={{ flexDirection: "column", padding: "40px 60px", gap: 28 }}>
          <div style={{ opacity: fade(frame, 0, 20), textAlign: "center" }}>
            <div style={{ fontFamily: "monospace", fontSize: 16, color: C.acc, letterSpacing: "0.1em", marginBottom: 12 }}>9 TOOLS</div>
            <div style={{ fontSize: 42, fontWeight: 700, color: C.text, lineHeight: 1.2 }}>Every trade. Every region.</div>
          </div>

          <div style={{ display: "flex", flexWrap: "wrap", gap: 12, justifyContent: "center" }}>
            {[
              { label: "Compliance", pro: false, delay: 20 },
              { label: "Code reference", pro: false, delay: 25 },
              { label: "Toolbox talks", pro: false, delay: 30 },
              { label: "Calc", pro: true, delay: 35 },
              { label: "Safety checklist", pro: true, delay: 40 },
              { label: "RAMS", pro: true, delay: 45 },
              { label: "Material check", pro: true, delay: 50 },
              { label: "Inspection", pro: true, delay: 55 },
              { label: "Photo / Visual", pro: true, delay: 60 },
            ].map(t => <Tool key={t.label} {...t} frame={frame} />)}
          </div>

          {/* Photo highlight */}
          <div style={{ opacity: fade(frame, 65, 80), transform: `translateY(${slideUp(frame, 65, 80)})`, textAlign: "center", background: "rgba(6,182,212,0.08)", border: "1px solid rgba(6,182,212,0.25)", borderRadius: 12, padding: "16px 32px" }}>
            <div style={{ fontSize: 20, color: C.acc2, fontWeight: 600 }}>
              📷 Upload a site photo — get a compliance verdict. First tool of its kind in the market.
            </div>
          </div>

          <Sub frame={frame} start={0} end={45} text="9 tools from compliance check to RAMS generation." />
          <Sub frame={frame} start={45} end={90} text="Visual compliance — upload a photo, get an instant verdict." />
        </AbsoluteFill>
      </Sequence>

      {/* ── SCENE 4: 87 regions (300-389) ───────────────────────────────── */}
      <Sequence from={300} durationInFrames={90}>
        <AbsoluteFill style={{ flexDirection: "column", padding: "40px 60px", gap: 28 }}>
          <div style={{ opacity: fade(frame, 0, 20), textAlign: "center" }}>
            <div style={{ fontFamily: "monospace", fontSize: 16, color: C.acc, letterSpacing: "0.1em", marginBottom: 12 }}>87 REGIONS</div>
            <div style={{ fontSize: 42, fontWeight: 700, color: C.text }}>Your jurisdiction.<br/><span style={{ color: C.acc }}>Your standard.</span></div>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 16 }}>
            {[
              { flag: "🇦🇺", name: "Australia", std: "AS/NZS 3000 · NCC", delay: 20 },
              { flag: "🇺🇸", name: "USA · all 50 states", std: "NEC NFPA 70 · OSHA", delay: 28 },
              { flag: "🇨🇦", name: "Canada", std: "CEC CSA C22.1", delay: 36 },
              { flag: "🇬🇧", name: "UK", std: "BS 7671 · CDM 2015", delay: 44 },
              { flag: "🇪🇺", name: "EU · 14 countries", std: "EN · IEC 60364", delay: 52 },
            ].map(r => <Region key={r.name} {...r} frame={frame} />)}
          </div>

          <Sub frame={frame} start={0} end={45} text="87 regions — the correct standard for where you're standing." />
          <Sub frame={frame} start={45} end={90} text="AU, UK, USA all 50 states, Canada, and 14 EU countries." />
        </AbsoluteFill>
      </Sequence>

      {/* ── SCENE 5: CTA (390-449) ──────────────────────────────────────── */}
      <Sequence from={390} durationInFrames={60}>
        <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", flexDirection: "column", gap: 28 }}>
          <div style={{ position: "absolute", inset: 0, background: "radial-gradient(ellipse 80% 60% at 50% 50%, rgba(59,130,246,0.12) 0%, transparent 70%)" }} />

          {/* Logo */}
          <div style={{ opacity: fade(frame, 0, 20), display: "flex", alignItems: "center", gap: 16 }}>
            <div style={{ width: 72, height: 72, borderRadius: 18, background: "linear-gradient(135deg,#3b82f6,#06b6d4)", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "monospace", fontSize: 28, fontWeight: 700, color: "white" }}>LL</div>
            <div style={{ fontFamily: "monospace", fontSize: 38, fontWeight: 700, color: C.text }}>legis-link</div>
          </div>

          {/* Free badge */}
          <div style={{ opacity: fade(frame, 20, 40), transform: `translateY(${slideUp(frame, 20, 40)})`, textAlign: "center" }}>
            <div style={{ fontSize: 52, fontWeight: 700, color: C.text, marginBottom: 8 }}>
              Free to try.<br/><span style={{ color: C.acc }}>No install. No account.</span>
            </div>
            <div style={{ fontSize: 24, color: C.muted2 }}>50 queries/day free · Pro $199/year</div>
          </div>

          {/* URL */}
          <div style={{ opacity: fade(frame, 35, 55), transform: `translateY(${slideUp(frame, 35, 55)})`, background: C.surf, border: `1px solid ${C.acc}`, borderRadius: 14, padding: "16px 40px", textAlign: "center" }}>
            <div style={{ fontFamily: "monospace", fontSize: 20, color: C.acc, marginBottom: 4 }}>legis-link-mcp-production-3e9b.up.railway.app/app</div>
            <div style={{ fontSize: 16, color: C.muted2 }}>Open on any phone · Works right now</div>
          </div>

          {/* Trade strip */}
          <div style={{ opacity: fade(frame, 45, 60), display: "flex", gap: 10, flexWrap: "wrap", justifyContent: "center", padding: "0 60px" }}>
            {["⚡ Electrical","🔧 Plumbing","❄️ HVAC","🔥 Gas","⚙️ Welding","☀️ Solar","🚨 Fire","🪚 Carpentry","🧱 Concrete","🏠 Roofing"].map(t => (
              <div key={t} style={{ background: C.surf2, border: `1px solid ${C.border}`, borderRadius: 20, padding: "6px 14px", fontSize: 16, color: C.muted2 }}>{t}</div>
            ))}
          </div>

          <Sub frame={frame} start={0} end={35} text="Legis-Link — construction compliance in your pocket." />
          <Sub frame={frame} start={35} end={60} text="Free. No install. Any phone. Try it now." />
        </AbsoluteFill>
      </Sequence>

    </AbsoluteFill>
  );
};
