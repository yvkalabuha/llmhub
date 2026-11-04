// llmhub from C#/.NET with plain HttpClient (no SDK): chat, task submit/poll/result,
// lease with heartbeat, error object. Env: LLMHUB_URL (http://10.8.0.1:8095), LLMHUB_KEY.
using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

var baseUrl = Environment.GetEnvironmentVariable("LLMHUB_URL") ?? "http://10.8.0.1:8095";
var key = Environment.GetEnvironmentVariable("LLMHUB_KEY")
          ?? throw new InvalidOperationException("LLMHUB_KEY is not set");
var model = Environment.GetEnvironmentVariable("LLMHUB_MODEL") ?? "auto";
var leaseModel = Environment.GetEnvironmentVariable("LLMHUB_LEASE_MODEL") ?? "big";

// The server waits up to 480 s synchronously: the client timeout must be longer.
using var http = new HttpClient { BaseAddress = new Uri(baseUrl), Timeout = TimeSpan.FromMinutes(10) };
http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", key);

static StringContent Json(object body) =>
    new(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");

static string Header(HttpResponseMessage r, string name) =>
    r.Headers.TryGetValues(name, out var v) ? string.Join(",", v) : "";

// llmhub error object: { "error": { code, reason, retryable, retry_after_s, task_id } }
static async Task<JsonNode?> ErrorOf(HttpResponseMessage r) =>
    JsonNode.Parse(await r.Content.ReadAsStringAsync())?["error"];

var messages = new[] { new { role = "user", content = "Say hello" } };

// 1. synchronous chat
var chat = await http.PostAsync("/v1/chat/completions", Json(new { model, messages }));
if (chat.StatusCode == HttpStatusCode.ServiceUnavailable)
{
    var err = await ErrorOf(chat);
    // queue_wait_exceeded: the task keeps running; poll it by task_id after Retry-After
    Console.WriteLine($"503 {err?["code"]} task_id={err?["task_id"]} retry_after_s={err?["retry_after_s"]}");
    return 1;
}
chat.EnsureSuccessStatusCode();
var completion = JsonNode.Parse(await chat.Content.ReadAsStringAsync())!;
Console.WriteLine($"answer: {completion["choices"]![0]!["message"]!["content"]}");
Console.WriteLine($"backend: {Header(chat, "X-LLMHub-Backend")} task: {Header(chat, "X-LLMHub-Task")}");

// 2. asynchronous task with an Idempotency-Key (a retry re-sends the same key)
var submit = new HttpRequestMessage(HttpMethod.Post, "/v1/tasks")
{
    Content = Json(new { model, messages, llmhub = new { priority = "normal", deadline_s = 3600 } }),
};
submit.Headers.Add("Idempotency-Key", $"gamedev-{Guid.NewGuid()}");
submit.Headers.Add("X-LLMHub-Priority", "normal");
var submitted = await http.SendAsync(submit);
if (submitted.StatusCode != HttpStatusCode.Accepted && submitted.StatusCode != HttpStatusCode.OK)
{
    var err = await ErrorOf(submitted);
    Console.WriteLine($"submit failed: {err?["code"]} reason={err?["reason"]} retryable={err?["retryable"]}");
    return 1;
}
var taskId = JsonNode.Parse(await submitted.Content.ReadAsStringAsync())!["id"]!.GetValue<string>();
for (var i = 0; i < 600; i++)
{
    var st = JsonNode.Parse(await http.GetStringAsync($"/v1/tasks/{taskId}"))!;
    var status = st["status"]!.GetValue<string>();
    if (status is "completed" or "failed" or "cancelled") break;
    await Task.Delay(TimeSpan.FromSeconds(1));
}
var result = await http.GetAsync($"/v1/tasks/{taskId}/result");
if (!result.IsSuccessStatusCode)
{
    var err = await ErrorOf(result);
    Console.WriteLine($"task failed: {err?["code"]} reason={err?["reason"]}");
    return 1;
}
Console.WriteLine($"task {taskId}: {JsonNode.Parse(await result.Content.ReadAsStringAsync())!["choices"]![0]!["message"]!["content"]}");

// 3. explicit lease: renew more often than ttl_s, close with final=true
var lease = await http.PostAsync("/v1/leases", Json(new { model = leaseModel, ttl_s = 120 }));
if (lease.StatusCode == HttpStatusCode.Created)
{
    var leaseId = JsonNode.Parse(await lease.Content.ReadAsStringAsync())!["id"]!.GetValue<string>();
    (await http.PostAsync($"/v1/leases/{leaseId}/renew", null)).EnsureSuccessStatusCode();
    (await http.PostAsync($"/v1/leases/{leaseId}/close", Json(new { final = true }))).EnsureSuccessStatusCode();
    Console.WriteLine($"lease {leaseId}: renewed and closed");
}
else
{
    var err = await ErrorOf(lease);
    Console.WriteLine($"no lease: {err?["code"]}");
}
return 0;
