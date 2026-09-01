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

/**
 * An allow/deny list of class names in the pattern syntax of `java.io.ObjectInputFilter`.
 *
 * `ObjectInputFilter` itself is only available on Java 9 and later, while this branch still
 * supports Java 8, so callers that need to restrict which classes a Java-serialized stream may
 * instantiate use this instead. Only the class-name patterns are implemented; see
 * [[ClassNameFilter.fromPattern]] for exactly what is accepted.
 *
 * @param rules pattern terms in declaration order, each paired with whether it allows the
 *              names it matches
 */
private[spark] class ClassNameFilter(rules: Seq[(Boolean, String => Boolean)]) {

  /**
   * Whether a class named in a serialized stream may be resolved. Array names are decided by
   * their element type, as `ObjectInputFilter` also filters on the array component, and
   * primitives are always allowed: they carry no code and the stream format needs them.
   */
  def allows(className: String): Boolean = {
    ClassNameFilter.elementName(className) match {
      case None => true
      case Some(name) if ClassNameFilter.primitiveNames.contains(name) => true
      case Some(name) =>
        // First match wins, and a name matched by no term is allowed, both as in
        // ObjectInputFilter. A pattern that means to reject the rest ends with "!*".
        rules.collectFirst { case (allow, matches) if matches(name) => allow }.getOrElse(true)
    }
  }
}

private[spark] object ClassNameFilter {

  private val primitiveNames =
    Set("boolean", "byte", "char", "short", "int", "long", "float", "double", "void")

  /**
   * Parses an `ObjectInputFilter`-style pattern, or returns None for a pattern that filters
   * nothing (empty, or the single term `*`).
   *
   * Accepted terms, separated by `;` and each optionally prefixed with `!` to reject rather
   * than allow: `*` (any class), `some.pkg.*` (that package), `some.pkg.**` (that package and
   * its subpackages), and an exact class name.
   *
   * The resource limits (`maxdepth=`, `maxarray=`, ...) and the module patterns of the full
   * syntax are not implemented, and a pattern using them is rejected rather than silently
   * under-enforced: a filter that looks stricter than it is would be worse than none.
   *
   * @throws IllegalArgumentException if any term is outside the supported subset
   */
  def fromPattern(pattern: String): Option[ClassNameFilter] = {
    val terms = pattern.split(";").map(_.trim).filter(_.nonEmpty)
    if (terms.isEmpty || terms.sameElements(Array("*"))) {
      None
    } else {
      Some(new ClassNameFilter(terms.map(parseTerm).toSeq))
    }
  }

  private def parseTerm(term: String): (Boolean, String => Boolean) = {
    val allow = !term.startsWith("!")
    val body = if (allow) term else term.substring(1)
    def unsupported(why: String): Nothing = throw new IllegalArgumentException(
      s"Unsupported serialization filter term '$term': $why. This branch accepts only the " +
        "class-name patterns '*', 'pkg.*', 'pkg.**' and exact class names, each optionally " +
        "prefixed with '!'. Use '*' to disable filtering.")
    if (body.isEmpty) {
      unsupported("the term is empty")
    } else if (body.contains("=")) {
      unsupported("resource limits are not implemented")
    } else if (body.contains("/")) {
      unsupported("module patterns are not implemented")
    } else if (body == "*") {
      (allow, (_: String) => true)
    } else if (body.endsWith(".**")) {
      // Keep the trailing dot so that "a.b.**" does not match a class in package "a.bc".
      val prefix = body.dropRight(2)
      (allow, (name: String) => name.startsWith(prefix))
    } else if (body.endsWith(".*")) {
      val prefix = body.dropRight(1)
      (allow, (name: String) =>
        name.startsWith(prefix) && !name.substring(prefix.length).contains('.'))
    } else if (body.contains("*")) {
      unsupported("'*' is only supported as a whole term or as a '.*' / '.**' suffix")
    } else {
      (allow, (name: String) => name == body)
    }
  }

  /**
   * The name whose package decides the outcome for `className`: the element type for an array,
   * or the name itself otherwise. None when the array element type is a primitive.
   */
  private def elementName(className: String): Option[String] = {
    val stripped = className.dropWhile(_ == '[')
    if (stripped.length == className.length) {
      Some(className)
    } else if (stripped.startsWith("L") && stripped.endsWith(";")) {
      Some(stripped.substring(1, stripped.length - 1))
    } else {
      None
    }
  }
}
