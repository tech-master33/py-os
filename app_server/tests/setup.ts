import { applyD1Migrations } from "cloudflare:test";
import { env } from "cloudflare:workers";

const testEnv = env as Cloudflare.Env & {
  TEST_MIGRATIONS: Array<{ name: string; queries: string[] }>;
};

await applyD1Migrations(testEnv.DB, testEnv.TEST_MIGRATIONS);
