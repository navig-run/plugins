// Stripe webhook handler (fixture). Detection should find this route as evidence.
export async function POST() {
  return new Response("ok");
}
