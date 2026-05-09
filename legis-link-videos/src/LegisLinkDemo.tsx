import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, spring, Sequence } from "remotion";

export const LegisLinkDemo = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Animations
  const logoOpacity = interpolate(frame, [0, 20], [0, 1], { extrapolateRight: "clamp" });
  const logoScale = spring({ frame, fps, config: { damping: 12, stiffness: 80 } });
  const textOpacity = interpolate(frame, [25, 45], [0, 1], { extrapolateRight: "clamp" });
  const cardY = interpolate(frame, [40, 70], [60, 0], { extrapolateRight: "clamp" });
  const cardOpacity = interpolate(frame, [40, 70], [0, 1], { extrapolateRight: "clamp" });
  const answerOpacity = interpolate(frame, [90, 110], [0, 1], { extrapolateRight: "clamp" });
  const badgeScale = spring({ frame: frame - 110, fps, config: { damping: 8, stiffness: 120 } });

  return (
    <AbsoluteFill style={{ background: "#0a0f1a", fontFamily: "IBM Plex Sans, system-ui, sans-serif" }}>

      {/* Background grid */}
      <AbsoluteFill style={{
        backgroundImage: "linear-gradient(rgba(30,41,59,0.4) 1px, transparent 1px), linear-gradient(90deg, rgba(30,41,59,0.4) 1px, transparent 1px)",
        backgroundSize: "60px 60px",
      }} />

      {/* Glow */}
      <AbsoluteFill style={{
        background: "radial-gradient(ellipse 60% 40% at 50% 20%, rgba(59,130,246,0.12) 0%, transparent 70%)",
      }} />

      {/* Logo */}
      <div style={{
        position: "absolute", top: 60, left: 0, right: 0,
        display: "flex", justifyContent: "center", alignItems: "center", gap: 16,
        opacity: logoOpacity, transform: `scale(${logoScale})`,
      }}>
        <div style={{
          width: 64, height: 64, borderRadius: 16,
          background: "linear-gradient(135deg, #3b82f6, #06b6d4)",
          display: "flex", alignItems: "center", justifyContent: "center",
          fontFamily: "monospace", fontSize: 26, fontWeight: 700, color: "white",
        }}>LL</div>
        <div style={{ fontFamily: "monospace", fontSize: 32, fontWeight: 700, color: "#f1f5f9", letterSpacing: -1 }}>
          legis-link
        </div>
      </div>

      {/* Tagline */}
      <div style={{
        position: "absolute", top: 155, left: 0, right: 0, textAlign: "center",
        opacity: textOpacity,
        fontFamily: "monospace", fontSize: 16, color: "#94a3b8", letterSpacing: "0.08em",
      }}>
        CONSTRUCTION COMPLIANCE AI · FREE ON MOBILE
      </div>

      {/* Question card */}
      <div style={{
        position: "absolute", top: 220, left: 80, right: 80,
        background: "#111827", border: "1px solid #1e293b", borderRadius: 16,
        padding: "28px 32px",
        opacity: cardOpacity, transform: `translateY(${cardY}px)`,
      }}>
        <div style={{ fontFamily: "monospace", fontSize: 13, color: "#3b82f6", marginBottom: 12, letterSpacing: "0.06em" }}>
          ELECTRICAL · NSW · JOURNEYMAN
        </div>
        <div style={{ fontSize: 22, color: "#f1f5f9", fontWeight: 500, lineHeight: 1.4 }}>
          Wire size for 45A load, 30m run, 240V single phase?
        </div>
      </div>

      {/* Answer card */}
      <div style={{
        position: "absolute", top: 400, left: 80, right: 80,
        background: "#1a2235", border: "1px solid #1e293b", borderRadius: 16,
        padding: "28px 32px",
        opacity: answerOpacity,
      }}>
        <div style={{ fontSize: 42, fontWeight: 700, color: "#3b82f6", fontFamily: "monospace", marginBottom: 8 }}>
          10mm²
        </div>
        <div style={{ fontSize: 18, color: "#f1f5f9", marginBottom: 8 }}>
          copper conductor required
        </div>
        <div style={{ fontSize: 14, color: "#94a3b8", marginBottom: 16 }}>
          Voltage drop: 1.97% — within 3% sub-circuit limit
        </div>

        {/* Status badge */}
        <div style={{
          display: "inline-flex", alignItems: "center", gap: 8,
          background: "rgba(16,185,129,0.12)", border: "1px solid rgba(16,185,129,0.3)",
          borderRadius: 8, padding: "6px 16px",
          transform: `scale(${Math.min(badgeScale, 1)})`,
          transformOrigin: "left center",
        }}>
          <div style={{ width: 8, height: 8, borderRadius: "50%", background: "#10b981" }} />
          <span style={{ fontFamily: "monospace", fontSize: 14, color: "#10b981", fontWeight: 600 }}>COMPLIANT</span>
        </div>

        <div style={{ marginTop: 12, fontFamily: "monospace", fontSize: 11, color: "#64748b" }}>
          Ref: AS/NZS 3008.1.1:2017 Table C1 · AS/NZS 3000:2018 Clause 3.6.2
        </div>
      </div>

      {/* Bottom URL */}
      <Sequence from={130}>
        <div style={{
          position: "absolute", bottom: 40, left: 0, right: 0, textAlign: "center",
          fontFamily: "monospace", fontSize: 14, color: "#64748b",
          opacity: interpolate(frame, [130, 150], [0, 1], { extrapolateRight: "clamp" }),
        }}>
          legis-link-mcp-production-3e9b.up.railway.app/app · Free · No install
        </div>
      </Sequence>

    </AbsoluteFill>
  );
};
