/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.apache.spark.serializer

import org.apache.spark.SparkFunSuite
import org.apache.spark.internal.config.Deploy.RECOVERY_SERIALIZATION_FILTER

class ClassNameFilterSuite extends SparkFunSuite {

  private def filter(pattern: String): ClassNameFilter =
    ClassNameFilter.fromPattern(pattern).get

  test("patterns that filter nothing produce no filter") {
    assert(ClassNameFilter.fromPattern("*").isEmpty)
    assert(ClassNameFilter.fromPattern("  *  ").isEmpty)
    assert(ClassNameFilter.fromPattern("").isEmpty)
    assert(ClassNameFilter.fromPattern(" ; ").isEmpty)
  }

  test("the default recovery pattern allows what the master persists and nothing else") {
    val f = filter(RECOVERY_SERIALIZATION_FILTER.defaultValue.get)
    assert(f.allows("java.lang.String"))
    assert(f.allows("java.util.HashMap"))
    assert(f.allows("scala.collection.immutable.List"))
    assert(f.allows("org.apache.spark.deploy.master.ApplicationInfo"))
    assert(f.allows("org.apache.spark.resource.ResourceProfile"))
    assert(!f.allows("org.apache.commons.lang3.tuple.ImmutablePair"))
    assert(!f.allows("javax.naming.ldap.Rdn"))
    assert(!f.allows("bsh.Interpreter"))
  }

  test("arrays are decided by their element type") {
    val f = filter("org.apache.spark.**;!*")
    assert(f.allows("[Lorg.apache.spark.deploy.master.WorkerInfo;"))
    assert(f.allows("[[Lorg.apache.spark.deploy.Command;"))
    assert(!f.allows("[Lbsh.Interpreter;"))
    // Primitive element types carry no code, and the stream format needs them.
    assert(f.allows("[I"))
    assert(f.allows("[[J"))
    assert(f.allows("[B"))
  }

  test("primitive type names are always allowed") {
    val f = filter("!*")
    Seq("boolean", "byte", "char", "short", "int", "long", "float", "double", "void")
      .foreach(name => assert(f.allows(name), s"$name should be allowed"))
    assert(!f.allows("java.lang.String"))
  }

  test("a package wildcard does not leak into a similarly named package") {
    val f = filter("a.b.**;!*")
    assert(f.allows("a.b.C"))
    assert(f.allows("a.b.c.D"))
    assert(!f.allows("a.bc.D"))
  }

  test("single-level and exact patterns") {
    val single = filter("a.b.*;!*")
    assert(single.allows("a.b.C"))
    assert(!single.allows("a.b.c.D"))

    val exact = filter("a.b.C;!*")
    assert(exact.allows("a.b.C"))
    assert(!exact.allows("a.b.CD"))
    assert(!exact.allows("a.b.D"))
  }

  test("the first matching term decides") {
    val f = filter("!a.b.Secret;a.b.**;!*")
    assert(!f.allows("a.b.Secret"))
    assert(f.allows("a.b.Other"))

    // Reversing the order shows the ordering is what makes the difference.
    val reversed = filter("a.b.**;!a.b.Secret;!*")
    assert(reversed.allows("a.b.Secret"))
  }

  test("a name matched by no term is allowed") {
    val f = filter("!a.b.**")
    assert(!f.allows("a.b.C"))
    assert(f.allows("x.y.Z"))
  }

  test("unsupported pattern syntax is rejected rather than partially applied") {
    // Silently ignoring these would leave a filter that looks stricter than it is.
    Seq("maxdepth=5", "java.**;maxarray=100", "!maxbytes=1", "mod/pkg.Class", "a.*.b", "!")
      .foreach { pattern =>
        val e = intercept[IllegalArgumentException](ClassNameFilter.fromPattern(pattern))
        assert(e.getMessage.contains("Unsupported serialization filter term"),
          s"unexpected message for '$pattern': ${e.getMessage}")
      }
  }
}
