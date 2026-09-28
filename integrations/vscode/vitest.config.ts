import { defineConfig } from "vitest/config";

// ABP_INSTALL_POINTER: never read the developer's real ~/.abp/install.json in a test.
export default defineConfig({
  test: {
    include: ["test/unit/**/*.test.ts"],
    testTimeout: 60_000,
    env: { ABP_INSTALL_POINTER: "/nonexistent/abp-install.json", ABP_CODE_ROOT: "" },
  },
});
