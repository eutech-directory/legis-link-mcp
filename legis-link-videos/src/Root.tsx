import { Composition } from "remotion";
import { LegisLinkDemo } from "./LegisLinkDemo";
import { LegisLinkProDemo } from "./LegisLinkProDemo";

export const RemotionRoot = () => {
  return (
    <>
      <Composition
        id="LegisLinkDemo"
        component={LegisLinkDemo}
        durationInFrames={180}
        fps={30}
        width={1080}
        height={1080}
      />
      <Composition
        id="LegisLinkProDemo"
        component={LegisLinkProDemo}
        durationInFrames={450}
        fps={30}
        width={1920}
        height={1080}
      />
    </>
  );
};
