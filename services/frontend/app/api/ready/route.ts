import { NextResponse } from "next/server";

export async function GET() {
  const agentUrl = process.env.AGENT_INTERNAL_URL;

  if (!agentUrl) {
    return NextResponse.json(
      {
        status: "not ready",
        reason: "AGENT_INTERNAL_URL is missing",
      },
      { status: 503 }
    );
  }

  try {
    const response = await fetch(`${agentUrl}/health`, {
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });

    if (!response.ok) {
      return NextResponse.json(
        {
          status: "not ready",
          reason: "Agent is unavailable",
        },
        { status: 503 }
      );
    }

    return NextResponse.json({ status: "ready" });
  } catch {
    return NextResponse.json(
      {
        status: "not ready",
        reason: "Cannot reach Agent",
      },
      { status: 503 }
    );
  }
}
