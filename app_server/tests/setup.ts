import { applyD1Migrations } from "cloudflare:test";
import { env } from "cloudflare:workers";

const testEnv = env as Cloudflare.Env & {
  TEST_MIGRATIONS: Array<{ name: string; queries: string[] }>;
  TEST_PACKAGE_MIGRATIONS: Array<{ name: string; queries: string[] }>;
};

await applyD1Migrations(testEnv.DB, testEnv.TEST_MIGRATIONS);
for (const shard of [
  testEnv.APP_DB_0!,
  testEnv.APP_DB_1!,
  testEnv.APP_DB_2!,
  testEnv.APP_DB_3!,
  testEnv.APP_DB_4!,
  testEnv.APP_DB_5!,
  testEnv.APP_DB_6!,
]) {
  await applyD1Migrations(shard, testEnv.TEST_PACKAGE_MIGRATIONS);
}
