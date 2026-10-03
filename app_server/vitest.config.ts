import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { readD1Migrations } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

export default defineConfig(async () => {
  const migrations = await readD1Migrations("./migrations");
  const packageMigrations = await readD1Migrations("./migrations/package_storage");

  return defineConfig({
    plugins: [
      cloudflareTest({
        wrangler: { configPath: "./wrangler.test.jsonc" },
        miniflare: {
          bindings: {
            PUBLISH_TOKEN: "test-token",
            TEST_MIGRATIONS: migrations,
            TEST_PACKAGE_MIGRATIONS: packageMigrations,
          },
        },
      }),
    ],
    test: {
      include: ["tests/**/*.test.ts"],
      setupFiles: ["./tests/setup.ts"],
    },
  });
});
