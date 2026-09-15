import { describe, expect, it } from "vitest";
import { buildPlatform } from "../../src/platform.js";
import { runManagedAgent } from "../../src/pi/agent-runner.js";

describe("real DeepSeek Agent Loop", () => {
  it("uses a governed tool and returns an answer without any model fake", async () => {
    const platform = await buildPlatform({ connectMcp: false });
    try {
      const result = await runManagedAgent(platform, {
        prompt: "查询订单 ORD-A-1001 的状态，并说明你实际调用了什么工具。",
        userId: "live-user",
        tenantId: "tenant_a",
        roles: ["supervisor"],
      });
      expect(result.final_answer.length).toBeGreaterThan(0);
      expect(platform.store.listAudit(result.trace_id).some((item) => item.tool_name === "order.get_status")).toBe(true);
    } finally {
      await platform.close();
    }
  }, 120_000);
});
