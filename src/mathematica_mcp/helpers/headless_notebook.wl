(* ::Package:: *)
(* headless_notebook.wl — notebook sessions with no front end *)
(*
   The addon's notebook commands drive a LIVE WINDOW: NotebookOpen,
   CreateDocument, SelectedNotebook. All of them need a front end, so on a
   headless host (a container, an SSH session, a batch node) every one of them
   fails and the server can only evaluate loose expressions.

   Here a "notebook" is instead the Notebook[...] expression on disk. That is
   enough to do the thing that actually matters headlessly: replay a real .nb
   cell by cell in the persistent kernel, in document order, with results and
   messages captured per cell.

   Design notes:
   - Cells are located by POSITION in the original expression, never by
     rebuilding it. Round-tripping through a hand-rolled box-to-text converter
     is what corrupts \[Gamma] and friends; we never do that conversion.
   - A cell is evaluated exactly as Shift+Enter would:
       ToExpression[content /. BoxData[b_] :> b, StandardForm]
     Retyping code read off a preview is a transcription step with no safety
     net, and its failure mode (silently unevaluated) is easy to misread.
   - Nothing here opens a front end, and nothing rasterises by default.
*)

BeginPackage["MCPHeadlessNotebook`"];

MCPOpen::usage = "MCPOpen[id, path] loads a .nb into a headless session.";
MCPCells::usage = "MCPCells[id, offset, limit, includeContent, style] lists cells; style \"\" means all.";
MCPEvaluateCell::usage = "MCPEvaluateCell[id, index, timeout] evaluates one cell.";
MCPEvaluateRange::usage = "MCPEvaluateRange[id, from, to, timeout, stopOnError] evaluates a span of cells.";
MCPWriteCell::usage = "MCPWriteCell[id, content, style, position, anchor] inserts a cell.";
MCPDeleteCell::usage = "MCPDeleteCell[id, index] removes a cell.";
MCPSave::usage = "MCPSave[id, path] writes the session's notebook expression to disk.";
MCPCreate::usage = "MCPCreate[id, path, title] starts an empty headless notebook.";
MCPClose::usage = "MCPClose[id] discards a session.";
MCPList::usage = "MCPList[] lists open headless sessions.";
MCPBindNotebookDirectory::usage = "MCPBindNotebookDirectory[dir, path] makes NotebookDirectory[] and friends resolve without a front end.";

Begin["`Private`"];

If[!ValueQ[$Sessions], $Sessions = <||>];
(* Output longer than this is truncated per cell; the full value stays in the
   kernel, so a caller that needs it can ask for the variable directly. *)
If[!ValueQ[$MaxOutputChars], $MaxOutputChars = 20000];

truncate[s_String] := If[StringLength[s] > $MaxOutputChars,
  StringTake[s, $MaxOutputChars] <> "\n... [truncated, " <>
    ToString[StringLength[s] - $MaxOutputChars] <> " more chars]",
  s
];
truncate[x_] := truncate[ToString[x]];

(* Compact: the default pretty-printer emits tabs and newlines, which have to
   survive OutputForm rendering and the transport intact before Python can
   parse them. One line has no such failure mode. *)
json[assoc_] := ExportString[assoc, "RawJSON", "Compact" -> True];
err[msg_String, extra_: <||>] := json[Join[<|"success" -> False, "error" -> msg|>, extra]];
ok[assoc_] := json[Join[<|"success" -> True|>, assoc]];

(* ------------------------------------------------------------------------ *)
(* Cell addressing                                                           *)
(* ------------------------------------------------------------------------ *)

(* Positions of content cells, in document order. A Cell whose first argument
   is CellGroupData is a collapsible GROUP WRAPPER, not content — it exists to
   hold other cells, and evaluating or editing it is never what the caller
   means. Position's depth-first order matches document order here. *)
leafPositions[nb_] := Select[
  Position[nb, _Cell],
  !MatchQ[Extract[nb, #], Cell[_CellGroupData, ___]] &
];

(* Cell[content, style, opts...] — the style is the SECOND argument. Scanning
   level 1 for the first string instead returns the CONTENT of a prose cell
   (Cell["some text", "Text"]), which silently mislabels every text cell. *)
cellStyle[c_] := If[Length[c] >= 2 && StringQ[c[[2]]], c[[2]], "Unknown"];

(* Plain text of a cell, for previews and prose cells. Strings buried in boxes
   are joined; this is deliberately approximate and never used as the source
   for evaluation. *)
cellText[c_] := Module[{content},
  content = If[Length[c] >= 1, First[c], ""];
  Which[
    StringQ[content], content,
    True, StringJoin[Cases[content, _String, Infinity]]
  ]
];

executableQ[c_] := MemberQ[{"Input", "Code"}, cellStyle[c]];

(* HoldRest is load-bearing: without it WL evaluates `body` before sessionOr is
   entered, so the guard runs AFTER the thing it guards. Read-only callers get
   away with it, but MCPClose deletes the session in its body and the guard then
   reports the session missing - closing always "failed" while actually
   succeeding. Holding the body makes the check happen first, for all callers. *)
SetAttributes[sessionOr, HoldRest];
sessionOr[id_, body_] := If[KeyExistsQ[$Sessions, id], body, err["No such headless notebook session: " <> ToString[id]]];

(* ------------------------------------------------------------------------ *)
(* Front-end-free notebook context                                           *)
(* ------------------------------------------------------------------------ *)

(* Without a front end NotebookDirectory[] returns $Failed, so a cell that
   opens with SetDirectory[NotebookDirectory[]] aborts the whole replay.
   Patching each notebook by hand is the usual workaround; binding the symbols
   once is the same fix applied in one place. These symbols carry no meaning
   headless, so overriding them costs nothing. *)
MCPBindNotebookDirectory[dir_String, path_String] := Module[{},
  (* Define the EXACT zero-argument forms. A NotebookDirectory[___] catch-all
     is less specific than the built-in NotebookDirectory[] rule, so it sorts
     after it and never fires; the exact form replaces the built-in rule
     outright, which is what we want. *)
  Quiet[
    Unprotect[System`NotebookDirectory, System`NotebookFileName];
    System`NotebookDirectory[] := dir;
    System`NotebookDirectory[_] := dir;
    System`NotebookFileName[] := path;
    System`NotebookFileName[_] := path;
    Protect[System`NotebookDirectory, System`NotebookFileName];
  ];
  dir
];

(* ------------------------------------------------------------------------ *)
(* Sessions                                                                  *)
(* ------------------------------------------------------------------------ *)

MCPOpen[id_String, path_String] := Module[{nb, abs, dir},
  abs = ExpandFileName[path];
  If[!FileExistsQ[abs], Return[err["File not found", <|"path" -> abs|>]]];
  (* Get, not Import: a .nb file IS a Notebook[...] expression, and Get returns
     it verbatim with BoxData intact. Import normalises some of that away. *)
  nb = Quiet[Check[Get[abs], $Failed]];
  If[!MatchQ[nb, _Notebook],
    Return[err[
      "File did not parse as a Notebook expression (Get returns Null on a truncated or malformed file)",
      <|"path" -> abs, "head" -> ToString[Head[nb]]|>
    ]]
  ];
  dir = DirectoryName[abs];
  $Sessions[id] = <|"path" -> abs, "dir" -> dir, "nb" -> nb, "dirty" -> False|>;
  ok[<|
    "id" -> id, "path" -> abs, "directory" -> dir,
    "cell_count" -> Length[leafPositions[nb]],
    "code_cells" -> Count[Extract[nb, #] & /@ leafPositions[nb], _?executableQ],
    "headless" -> True
  |>]
];

MCPCreate[id_String, path_String, title_String] := Module[{nb, cells},
  cells = If[title === "", {}, {Cell[title, "Title"]}];
  nb = Notebook[cells];
  $Sessions[id] = <|
    "path" -> If[path === "", "", ExpandFileName[path]],
    "dir" -> If[path === "", Directory[], DirectoryName[ExpandFileName[path]]],
    "nb" -> nb, "dirty" -> True
  |>;
  ok[<|"id" -> id, "path" -> $Sessions[id, "path"], "cell_count" -> Length[cells], "headless" -> True|>]
];

MCPClose[id_String] := sessionOr[id, ($Sessions = KeyDrop[$Sessions, id]; ok[<|"id" -> id, "closed" -> True|>])];

MCPList[] := ok[<|
  "notebooks" -> KeyValueMap[
    <|"id" -> #1, "path" -> #2["path"], "cell_count" -> Length[leafPositions[#2["nb"]]], "dirty" -> #2["dirty"]|> &,
    $Sessions
  ],
  "headless" -> True
|>];

(* Kept so a kernel still holding an older caller keeps working. *)
MCPCells[id_String, offset_Integer, limit_Integer, includeContent : (True | False)] :=
  MCPCells[id, offset, limit, includeContent, ""];

MCPCells[id_String, offset_Integer, limit_Integer, includeContent : (True | False), style_String] :=
  sessionOr[id, Module[{nb, pos, keep, cells, slice, upper},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    (* Filter BEFORE slicing: filtering a page would silently drop matches that
       fall outside it. Indices stay notebook-wide, because callers evaluate by
       them - renumbering to the filtered order would break that. *)
    keep = Range[Length[pos]];
    If[style =!= "", keep = Select[keep, cellStyle[Extract[nb, pos[[#]]]] === style &]];
    upper = If[limit <= 0, Length[keep], Min[Length[keep], offset + limit]];
    slice = If[upper >= offset + 1, Take[keep, {offset + 1, upper}], {}];
    cells = Table[
      Module[{c = Extract[nb, pos[[i]]], txt},
        txt = cellText[c];
        Join[
          <|
            "index" -> i - 1,
            "style" -> cellStyle[c],
            "executable" -> executableQ[c],
            "chars" -> StringLength[txt]
          |>,
          If[includeContent, <|"content" -> truncate[txt]|>, <|"preview" -> StringTake[txt, Min[80, StringLength[txt]]]|>]
        ]
      ],
      {i, slice}
    ];
    ok[<|"id" -> id, "total" -> Length[keep], "offset" -> offset,
        "style" -> style, "cells" -> cells|>]
  ]];

(* ------------------------------------------------------------------------ *)
(* Evaluation                                                                *)
(* ------------------------------------------------------------------------ *)

(* Evaluate one cell the way Shift+Enter would, capturing value, Print output,
   messages and timing. Print is redirected to a temp file rather than left on
   stdout: package-heavy notebooks print constantly, and on the cold transport
   that text lands in the middle of the JSON the caller has to parse. *)
evalCell[c_, dir_String, path_String, timeout_] := Module[
  {boxes, res, msgs, t0, printed = "", stream, tmp, aborted = False},

  If[!executableQ[c],
    Return[<|"success" -> True, "skipped" -> True, "reason" -> "not an Input/Code cell"|>]
  ];

  MCPBindNotebookDirectory[dir, path];
  boxes = First[c] /. BoxData[b_] :> b;
  tmp = FileNameJoin[{$TemporaryDirectory, "mcp-print-" <> ToString[$ProcessID] <> "-" <> ToString[RandomInteger[10^9]] <> ".txt"}];
  t0 = AbsoluteTime[];

  stream = Quiet[Check[OpenWrite[tmp], $Failed]];
  Block[{$MessageList = {}},
    res = If[stream === $Failed,
      TimeConstrained[ToExpression[boxes, StandardForm], timeout, $MCPAborted],
      Block[{$Output = {stream}}, TimeConstrained[ToExpression[boxes, StandardForm], timeout, $MCPAborted]]
    ];
    msgs = $MessageList;
  ];
  If[stream =!= $Failed,
    Quiet[Close[stream]];
    printed = Quiet[Check[Import[tmp, "Text"], ""]];
    Quiet[DeleteFile[tmp]];
  ];
  If[!StringQ[printed], printed = ""];
  aborted = (res === $MCPAborted);

  <|
    "success" -> !aborted,
    "timed_out" -> aborted,
    "output" -> If[aborted, "", truncate[ToString[res, InputForm]]],
    "printed" -> truncate[printed],
    "messages" -> (ToString[#, InputForm] & /@ msgs),
    "timing_ms" -> Round[(AbsoluteTime[] - t0) * 1000]
  |>
];

MCPEvaluateCell[id_String, index_Integer, timeout_] :=
  sessionOr[id, Module[{nb, pos, c},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    If[index < 0 || index >= Length[pos],
      Return[err["Cell index out of range", <|"index" -> index, "total" -> Length[pos]|>]]
    ];
    c = Extract[nb, pos[[index + 1]]];
    ok[Join[<|"id" -> id, "index" -> index, "style" -> cellStyle[c]|>,
            evalCell[c, $Sessions[id, "dir"], $Sessions[id, "path"], timeout]]]
  ]];

(* Replay a span in document order. stopOnError halts at the first cell that
   times out, so a long notebook does not keep burning kernel time after the
   state it depends on has already failed to materialise. *)
MCPEvaluateRange[id_String, from_Integer, to_Integer, timeout_, stopOnError : (True | False)] :=
  sessionOr[id, Module[{nb, pos, results = {}, upper, c, r},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    upper = If[to < 0, Length[pos] - 1, Min[to, Length[pos] - 1]];
    If[from < 0 || from > upper,
      Return[err["Empty or invalid cell range", <|"from" -> from, "to" -> upper, "total" -> Length[pos]|>]]
    ];
    Do[
      c = Extract[nb, pos[[i + 1]]];
      r = evalCell[c, $Sessions[id, "dir"], $Sessions[id, "path"], timeout];
      AppendTo[results, Join[<|"index" -> i, "style" -> cellStyle[c]|>, r]];
      If[stopOnError && TrueQ[r["timed_out"]], Break[]],
      {i, from, upper}
    ];
    ok[<|
      "id" -> id, "from" -> from, "to" -> upper,
      "evaluated" -> Length[results],
      "results" -> results
    |>]
  ]];

(* ------------------------------------------------------------------------ *)
(* Mutation and persistence                                                  *)
(* ------------------------------------------------------------------------ *)

MCPWriteCell[id_String, content_String, style_String, position_String, anchor_Integer] :=
  sessionOr[id, Module[{nb, pos, newCell, cells, at, updated},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    newCell = Cell[BoxData[content], style];
    (* Insertion only rewrites the TOP-LEVEL cell list. Splicing into a nested
       CellGroupData would need the group's own position and is deliberately
       not attempted: silently putting a cell in the wrong group is worse than
       appending it where the caller can see it. *)
    cells = First[nb];
    If[!ListQ[cells], cells = {cells}];
    at = Switch[position,
      "Beginning", 0,
      "End", Length[cells],
      "Before", Max[0, Min[anchor, Length[cells]]],
      "After", Max[0, Min[anchor + 1, Length[cells]]],
      _, Length[cells]
    ];
    updated = Insert[cells, newCell, at + 1];
    $Sessions[id, "nb"] = ReplacePart[nb, 1 -> updated];
    $Sessions[id, "dirty"] = True;
    ok[<|"id" -> id, "inserted_at" -> at, "cell_count" -> Length[leafPositions[$Sessions[id, "nb"]]]|>]
  ]];

MCPDeleteCell[id_String, index_Integer] :=
  sessionOr[id, Module[{nb, pos},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    If[index < 0 || index >= Length[pos],
      Return[err["Cell index out of range", <|"index" -> index, "total" -> Length[pos]|>]]
    ];
    $Sessions[id, "nb"] = Delete[nb, pos[[index + 1]]];
    $Sessions[id, "dirty"] = True;
    ok[<|"id" -> id, "deleted" -> index, "cell_count" -> Length[leafPositions[$Sessions[id, "nb"]]]|>]
  ]];

(* Put, not Export: the session holds a real Notebook[...] expression and Put
   writes it back in the same textual form Mathematica itself uses, so a file
   opened, edited and saved here stays byte-comparable apart from the edit. *)
MCPSave[id_String, path_String] :=
  sessionOr[id, Module[{target, res},
    target = If[path === "", $Sessions[id, "path"], ExpandFileName[path]];
    If[target === "" || target === None,
      Return[err["No path to save to; pass one explicitly"]]
    ];
    Quiet[If[!DirectoryQ[DirectoryName[target]], CreateDirectory[DirectoryName[target]]]];
    res = Quiet[Check[Put[$Sessions[id, "nb"], target]; "ok", $Failed]];
    If[res === $Failed, Return[err["Failed to write notebook", <|"path" -> target|>]]];
    $Sessions[id, "path"] = target;
    $Sessions[id, "dirty"] = False;
    ok[<|"id" -> id, "path" -> target, "saved" -> True|>]
  ]];

End[];
EndPackage[];
